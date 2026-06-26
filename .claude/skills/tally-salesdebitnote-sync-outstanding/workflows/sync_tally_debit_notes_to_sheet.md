# Workflow: sync_tally_debit_notes_to_sheet

## Objective

Sync sales debit-note vouchers (booked against Sundry Debtors) from local Tally Prime into the outstanding-report Google Sheet for debit notes. Idempotent: re-running for an overlapping date range adds new rows only (no duplicates).

## Inputs

- **From date** (DD-MM-YYYY) — start of date range, user-supplied at runtime.
- **To date** (DD-MM-YYYY) — end of date range, user-supplied at runtime. Defaults to today if not given.
- **Companies** (optional, comma-separated) — restrict to a subset of currently loaded companies. Default: all loaded companies.
- **Project `.env`** (in the cwd) must define:
  - `TALLY_HOST` — e.g. `http://localhost:9000`
  - `GOOGLE_CREDENTIALS_FILE` — path to OAuth client secrets JSON
  - `GOOGLE_TOKEN_FILE` — path to cached OAuth token JSON
  - `DEBIT_NOTE_SHEET_URL` — full URL of the destination Google Sheet
  - `DEBIT_NOTE_SHEET_TAB` — tab name within the sheet (e.g. `Sheet1`)

## Chunking rule

**Always fetch one calendar month at a time, even if the user requests a multi-month range.** Tally's HTTP endpoint has a 180s read timeout in the fetch tool, and ranges longer than ~1 month (especially across multiple loaded companies) routinely exceed it; the failure also leaves Tally unresponsive for ~30s+, breaking subsequent calls.

For a multi-month request, run the fetch+push pair sequentially per month: `01-MM → end-of-month`, etc. (clamp the final chunk's `to` to the user's actual end date). The push step is idempotent on `(Company, Location, Voucher No.)`, so per-month chunks never produce duplicates.

## Tools (in order)

All paths below are relative to the skill folder (`<SKILL_DIR>` = `.claude/skills/tally-salesdebitnote-sync-outstanding/`).

1. **`tools/list_tally_companies.py`** — POSTs a Tally XML envelope to `$TALLY_HOST` requesting `List of Companies`. Prints a JSON array of company names to stdout.
   - CLI: no args.
   - Use the output to confirm with the user which companies will be included.

2. **`tools/fetch_tally_debit_notes.py --from <DD-MM-YYYY> --to <DD-MM-YYYY> [--companies "Co1,Co2"] --output <path>`**
   - Loops over each loaded (or filtered) company, sets `SVCURRENTCOMPANY`, lists every voucher type whose PARENT class is `Debit Note` (skipping names containing `purchase`/`purch`/`return`), and pulls all matching vouchers between `SVFROMDATE` and `SVTODATE`.
   - For each company also fetches the Sundry-Debtor ledger set (group-chain walk) and **drops vouchers whose party isn't a Sundry Debtor**. This is the authoritative filter.
   - Reads the column schema from `reference/columns.md` via `tools/_schema.py`.
   - Writes a JSON list-of-dicts to `<path>` (typically `.tmp/debit_notes_<timestamp>.json`).
   - Prints a one-line summary like `{"vouchers_scanned": 312, "vouchers_against_debtors": 27, "rows": 27, "companies": ["A","B"]}`.

3. **`tools/push_debit_notes_to_sheet.py --input <path>`**
   - Loads `DEBIT_NOTE_SHEET_URL`, `DEBIT_NOTE_SHEET_TAB`, and Google creds from `.env`.
   - Validates the sheet's row 1 against the schema; writes headers if blank, errors out on mismatch.
   - Reads existing `(Company, Location, Voucher No.)` keys; appends only rows whose key is new.
   - Uses `RAW` value-input (per project convention) so phone-number-shaped strings and large numbers don't get coerced.
   - Prints summary JSON: `{"fetched": N, "appended": N, "skipped": N, "sheet_url": "..."}`.

## Outputs

- Appended rows in the destination Google Sheet (URL from `DEBIT_NOTE_SHEET_URL`).
- A summary printed to the terminal.
- An intermediate `.tmp/debit_notes_<timestamp>.json` file (disposable; safe to delete).

## Edge cases

- **Tally not running / port closed** → `list_tally_companies.py` exits non-zero with a connection error. Tell the user to start Tally Prime and confirm Help → Settings → Connectivity is set to "Both" on port 9000.
- **No companies loaded** → `list_tally_companies.py` returns `[]`. Tell the user to load at least one company in Tally and stop.
- **Empty date range** → `fetch_tally_debit_notes.py` may return 0 vouchers. Push step will still run (writes 0 rows) and the summary will say `appended: 0`.
- **`vouchers_scanned > 0` but `vouchers_against_debtors == 0`** — every debit note in the range was booked against a creditor (purchase-side adjustment). Expected and correct; nothing to push.
- **Google OAuth token expired or missing** → `push_debit_notes_to_sheet.py` opens the browser for re-auth on first run. If that fails non-interactively, delete `token.json` and re-run from a terminal that can open a browser.
- **Sheet header row mismatch** → `push_debit_notes_to_sheet.py` exits non-zero. Either fix the sheet's row 1 or update `reference/columns.md`.
- **Debit note without inventory** (interest, late-fee, ISD, rate-difference) → emit one row with blank `quantity`, `rate`, `value` and the voucher-level `gross_total`. The Narration column usually carries the descriptive text for these.
- **Voucher with no `<REFERENCE>`** (no original sales invoice) → `against_invoice` is blank. Common for non-goods debits.
- **Voucher numbers reused across companies** → dedupe key is the composite `(Company, Location, Voucher No.)`, so this is safe.
- **Re-run with overlapping date range** → expected and supported. Push step skips already-present rows; summary will show `skipped > 0`.

## Lessons learned (carried over from sibling tally-salescreditnote-sync-outstanding skill)

- **Don't use Tally's built-in `Debit Note Register` report — it filters to ONE master voucher type per company.** Instead, list every voucher type whose PARENT class is `Debit Note` for that company, then issue one `Voucher Register` query per type with explicit `<VOUCHERTYPENAME>`. This is more chatty (N HTTP calls per company) but actually returns all debit notes.
- **Use the older-style envelope** with `<TALLYREQUEST>Export Data</TALLYREQUEST>` and the `<EXPORTDATA><REQUESTDESC><REPORTNAME>...` wrapper. The newer `<TYPE>Data</TYPE><ID>...` form is unreliable for Voucher Register.
- **`Against Sales Invoice no.` comes from the voucher-level `<REFERENCE>` tag.** Same convention as the credit-note skill. Many sales-side debit notes (interest, late fees, ISD reversals) have no original invoice, so blank is expected.
- **`Type` column is the literal string `"debit note"`** for every row — not derived from voucher type.
- **Tally XML is dirty.** Raw control bytes, stray Windows-1252 bytes, and numeric character references like `&#0;` need sanitization before parsing — see `_sanitize_tally_xml` in the fetch tool.
- **Voucher-level gross total isn't a single XML tag.** Compute it as `abs(amount of the party's LEDGERENTRIES.LIST entry)`. For debit notes the party row is typically negative (debit) — `abs()` normalises it.
- **Output is voucher-aggregated, not item-wise.** One row per voucher; quantity and value are absolute sums across all inventory lines; rate is the weighted average. Vouchers with no inventory emit blanks for those three columns.
- **Dedupe key is the triple `(Company, Location, Voucher No.)`** — Location is needed because two raw Tally companies can share a display name (Surat + Noida branches both map to "O-tec").
- **Company / location come from `reference/companies.md`, not Tally.** Same mapping as the sibling outstanding-report skills — keep them aligned. **Refresh every April** when the financial-year suffix in Tally rolls over.
- **Cancelled vouchers are filtered out** via `_is_cancelled` (matches `ACTION="Cancel"` and `ISCANCELLED=Yes`).
- **Date column format is `DD/MM/YYYY`** (Indian convention, sortable as a date in Sheets). The internal `Month` column stays `YYYY-MM` because that sorts naturally as text.

## Lessons specific to this skill

- **`PARENT` class probe (verified against the live Tally on 2026-04-29):** every debit-note voucher type sits under exactly one parent class — `Debit Note` (singular, title case). All four loaded companies report types like `SALES DEBIT NOTE`, `GST DEBIT NOTE`, `GST DEBIT NOTE-ISD`, `DEBIT NOTE-194R FOC`, `TCS DEBIT NOTE`, `Debit Note` (built-in), plus the purchase-side ones we skip by name (`PURCHASE DEBIT NOTE`, `PURCHASE RETURN`).
- **Sundry-Debtor walk is mandatory.** Without it, debit notes booked against vendors (purchase-side adjustments) would pollute the receivables sheet. Even with the name-skip filter, voucher types like `Debit Note` or `GST DEBIT NOTE-ISD` can legitimately host either side; the party-side filter is the authoritative test.
- **`GST DEBIT NOTE-ISD` is intentionally NOT skipped by name.** ISD debit notes can be passed through to a debtor (rare, but happens for pass-through tax recoveries). Whether they're emitted depends entirely on the party-side filter.
- **Narration is captured** because non-goods debits (interest, late-fee, freight recovery) are best identified by their narration text — Quantity/Rate/Value will be blank and `against_invoice` is often blank too.
