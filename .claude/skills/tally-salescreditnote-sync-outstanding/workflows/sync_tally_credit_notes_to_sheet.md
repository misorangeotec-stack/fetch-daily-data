# Workflow: sync_tally_credit_notes_to_sheet

## Objective

Sync sales credit-note vouchers from local Tally Prime into the existing outstanding-report Google Sheet for credit notes. Idempotent: re-running for an overlapping date range adds new rows only (no duplicates).

## Inputs

- **From date** (DD-MM-YYYY) — start of date range, user-supplied at runtime.
- **To date** (DD-MM-YYYY) — end of date range, user-supplied at runtime. Defaults to today if not given.
- **Companies** (optional, comma-separated) — restrict to a subset of currently loaded companies. Default: all loaded companies.
- **Project `.env`** (in the cwd) must define:
  - `TALLY_HOST` — e.g. `http://localhost:9000`
  - `GOOGLE_CREDENTIALS_FILE` — path to OAuth client secrets JSON
  - `GOOGLE_TOKEN_FILE` — path to cached OAuth token JSON
  - `CREDIT_NOTE_SHEET_URL` — full URL of the destination Google Sheet
  - `CREDIT_NOTE_SHEET_TAB` — tab name within the sheet (e.g. `Sheet1`)

## Chunking rule

**Always fetch one calendar month at a time, even if the user requests a multi-month range.** Tally's HTTP endpoint has a 180s read timeout in the fetch tool, and ranges longer than ~1 month (especially across multiple loaded companies) routinely exceed it; the failure also leaves Tally unresponsive for ~30s+, breaking subsequent calls.

For a multi-month request, run the fetch+push pair sequentially per month: `01-MM → end-of-month`, etc. (clamp the final chunk's `to` to the user's actual end date). The push step is idempotent on `(Company, Location, Voucher No.)`, so per-month chunks never produce duplicates.

## Tools (in order)

All paths below are relative to the skill folder (`<SKILL_DIR>` = `.claude/skills/tally-salescreditnote-sync-outstanding/`).

1. **`tools/list_tally_companies.py`** — POSTs a Tally XML envelope to `$TALLY_HOST` requesting `List of Companies`. Prints a JSON array of company names to stdout.
   - CLI: no args.
   - Use the output to confirm with the user which companies will be included.

2. **`tools/fetch_tally_credit_notes.py --from <DD-MM-YYYY> --to <DD-MM-YYYY> [--companies "Co1,Co2"] --output <path>`**
   - Loops over each loaded (or filtered) company, sets `SVCURRENTCOMPANY`, lists every voucher type whose PARENT class is `Credit Note`, and pulls all matching vouchers between `SVFROMDATE` and `SVTODATE`.
   - Skips voucher types with "purchase" in the name (PURCHASE CREDIT NOTE belongs to vendor adjustments, not the receivables sheet).
   - Reads the column schema from `reference/columns.md` via `tools/_schema.py`.
   - Writes a JSON list-of-dicts to `<path>` (typically `.tmp/credit_notes_<timestamp>.json`).
   - Prints a one-line summary like `{"vouchers": 312, "rows": 312, "companies": ["A","B"]}`.

3. **`tools/push_credit_notes_to_sheet.py --input <path>`**
   - Loads `CREDIT_NOTE_SHEET_URL`, `CREDIT_NOTE_SHEET_TAB`, and Google creds from `.env`.
   - Validates the sheet's row 1 against the schema; writes headers if blank, errors out on mismatch.
   - Reads existing `(Company, Location, Voucher No.)` keys; appends only rows whose key is new.
   - Prints summary JSON: `{"fetched": N, "appended": N, "skipped": N, "sheet_url": "..."}`.

## Outputs

- Appended rows in the destination Google Sheet (URL from `CREDIT_NOTE_SHEET_URL`).
- A summary printed to the terminal.
- An intermediate `.tmp/credit_notes_<timestamp>.json` file (disposable; safe to delete).

## Edge cases

- **Tally not running / port closed** → `list_tally_companies.py` exits non-zero with a connection error. Tell the user to start Tally Prime and confirm Help → Settings → Connectivity is set to "Both" on port 9000.
- **No companies loaded** → `list_tally_companies.py` returns `[]`. Tell the user to load at least one company in Tally and stop.
- **Empty date range** → `fetch_tally_credit_notes.py` may return 0 vouchers. Push step will still run (writes 0 rows) and the summary will say `appended: 0`.
- **Google OAuth token expired or missing** → `push_credit_notes_to_sheet.py` opens the browser for re-auth on first run (uses the credentials file). If that fails non-interactively, delete `token.json` and re-run from a terminal that can open a browser.
- **Sheet header row mismatch** → `push_credit_notes_to_sheet.py` exits non-zero. Either fix the sheet's row 1 or update `reference/columns.md`.
- **GST CREDIT NOTE without inventory** (rate-difference adjustments) → emit one row with blank `quantity`, `rate`, `value` and the voucher-level `gross_total`. The reference Excel already shows nulls in these columns for these vouchers, so this matches the expected format.
- **Voucher with no `<REFERENCE>`** (user didn't enter one in Tally) → `against_invoice` is blank. This matches the reference Excel for vouchers like `CN/1/25-26`.
- **Voucher numbers reused across companies** → dedupe key is the composite `(Company, Location, Voucher No.)`, so this is safe.
- **Re-run with overlapping date range** → expected and supported. Push step skips already-present rows; summary will show `skipped > 0`.

## Lessons learned (from sibling tally-sales-sync-outstanding skill, applies here too)

- **Don't use Tally's built-in `Credit Note Register` report — it filters to ONE master voucher type per company.** Instead, list every voucher type whose PARENT class is `Credit Note` for that company, then issue one `Voucher Register` query per type with explicit `<VOUCHERTYPENAME>`. This is more chatty (N HTTP calls per company) but actually returns all credit notes.
- **Use the older-style envelope** with `<TALLYREQUEST>Export Data</TALLYREQUEST>` and the `<EXPORTDATA><REQUESTDESC><REPORTNAME>...` wrapper. The newer `<TYPE>Data</TYPE><ID>...` form is unreliable for Voucher Register.
- **`Against Sales Invoice no.` comes from the voucher-level `<REFERENCE>` tag.** This was verified against the reference Excel by matching `CN/2/25-26 → SPARE/24-25/2343`, `CN/3/25-26 → HAND/24-25/211`, and `G/SR/25-26/1 → SPARE/24-25/2479` exactly. Bill allocations (`LEDGERENTRIES.LIST/BILLALLOCATIONS.LIST/NAME`) often contain MULTIPLE invoice references for a single credit note — do NOT use those, the user-entered `REFERENCE` is the single source of truth for the "Against Sales Invoice no." column.
- **`Type` column is the literal string `"credit note"`** for every row — not derived from voucher type. The reference Excel uses this constant regardless of whether the voucher type is `GST CREDIT NOTE` or `GST SALES RETURN`.
- **Tally XML is dirty.** Raw control bytes, stray Windows-1252 bytes, and numeric character references like `&#0;` need sanitization before parsing — see `_sanitize_tally_xml` in the fetch tool.
- **Voucher-level gross total isn't a single XML tag.** Compute it as `abs(amount of the party's LEDGERENTRIES.LIST entry)`. For credit notes the party row is positive (we owe them money back), but `abs()` keeps it consistent with the sales sheet.
- **Output is voucher-aggregated, not item-wise.** One row per voucher; quantity and value are absolute sums across all inventory lines; rate is the weighted average. Vouchers with no inventory (rate-difference adjustments) emit blanks for those three columns.
- **Dedupe key is the triple `(Company, Location, Voucher No.)`** — Location is needed because two raw Tally companies can share a display name (Surat + Noida branches both map to "O-tec").
- **Company / location come from `reference/companies.md`, not Tally.** Same mapping as the sibling sales / bank-receipt / credit-limit skills — keep them aligned. **Refresh every April** when the financial-year suffix in Tally rolls over.
- **Cancelled vouchers are filtered out** via `_is_cancelled` (matches `ACTION="Cancel"` and `ISCANCELLED=Yes`).
- **Skip PURCHASE CREDIT NOTE** by name. It shares the `Credit Note` parent class but is a vendor-side adjustment that doesn't belong in the sales receivables sheet.
- **Date column format is `DD/MM/YYYY`** (Indian convention, sortable as a date in Sheets). The internal `Month` column stays `YYYY-MM` because that sorts naturally as text.
