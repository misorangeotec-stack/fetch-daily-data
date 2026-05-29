# Workflow: sync_tally_journals_to_sheet

## Objective

Sync journal vouchers (booked against Sundry Debtors) from local Tally Prime into the outstanding-report Google Sheet for journal entries. **Row granularity is per-allocation**: a journal that posts ₹10,00,000 against six original invoices yields six rows, each carrying that allocation's Reference Invoice Number and partial Amount. Each row carries a `Transaction Type` of `Dr` or `Cr` reflecting the side the debtor party leg sits on. Idempotent: re-running for an overlapping date range adds new rows only (no duplicates).

## Inputs

- **From date** (DD-MM-YYYY) — start of date range, user-supplied at runtime.
- **To date** (DD-MM-YYYY) — end of date range, user-supplied at runtime. Defaults to today if not given.
- **Companies** (optional, comma-separated) — restrict to a subset of currently loaded companies. Default: all loaded companies.
- **Project `.env`** (in the cwd) must define:
  - `TALLY_HOST` — e.g. `http://localhost:9000`
  - `GOOGLE_CREDENTIALS_FILE` — path to OAuth client secrets JSON
  - `GOOGLE_TOKEN_FILE` — path to cached OAuth token JSON
  - `JOURNAL_SHEET_URL` — full URL of the destination Google Sheet
  - `JOURNAL_SHEET_TAB` — tab name within the sheet (e.g. `Sheet1`)

## Chunking rule

**Always fetch one calendar month at a time, even if the user requests a multi-month range.** Tally's HTTP endpoint has a 180s read timeout in the fetch tool, and ranges longer than ~1 month (especially across multiple loaded companies) routinely exceed it; the failure also leaves Tally unresponsive for ~30s+, breaking subsequent calls.

For a multi-month request, run the fetch+push pair sequentially per month: `01-MM → end-of-month`, etc. (clamp the final chunk's `to` to the user's actual end date). The push step is idempotent on `(Company, Location, Voucher No., Particulars, Reference Invoice Number)`, so per-month chunks never produce duplicates.

## Tools (in order)

All paths below are relative to the skill folder (`<SKILL_DIR>` = `.claude/skills/tally-salesjournal-sync-outstanding/`).

1. **`tools/list_tally_companies.py`** — POSTs a Tally XML envelope to `$TALLY_HOST` requesting `List of Companies`. Prints a JSON array of company names to stdout.
   - CLI: no args.
   - Use the output to confirm with the user which companies will be included.

2. **`tools/fetch_tally_journals.py --from <DD-MM-YYYY> --to <DD-MM-YYYY> [--companies "Co1,Co2"] --output <path>`**
   - Loops over each loaded (or filtered) company, sets `SVCURRENTCOMPANY`, lists every voucher type whose PARENT class is `Journal` (or `Journals`), and pulls all matching vouchers between `SVFROMDATE` and `SVTODATE`.
   - For each company also fetches the **target ledger set** — every ledger rolling up to `Sundry Debtors` OR `Branch / Divisions` (group-chain walk). For every voucher, finds each LEDGERENTRIES leg whose ledger is in that target set and **emits one row per `BILLALLOCATIONS.LIST` entry under that leg** (each row carries the allocation's `NAME` as `Reference Invoice Number` and its `AMOUNT`). A leg with no allocations emits one `On Account` row with blank `ref_inv_no` and the full leg amount. Inter-branch legs typically carry no allocations, so they surface as `On Account` rows. Vouchers with no kept leg are skipped entirely.
   - Each emitted row's `Transaction Type` is derived from the leg's `ISDEEMEDPOSITIVE` (`Yes` → `Dr`, `No` → `Cr`); falls back to AMOUNT sign if absent. `Amount` is the unsigned absolute value of the allocation (or leg, for `On Account` rows).
   - Reads the column schema from `reference/columns.md` via `tools/_schema.py`.
   - Writes a JSON list-of-dicts to `<path>` (typically `.tmp/journals_<timestamp>.json`).
   - Prints a one-line summary like `{"vouchers_scanned": 411, "vouchers_with_kept_leg": 31, "rows": 60, "companies": ["A","B"]}`. (`kept` = at least one debtor or branch/divisions leg matched.)

3. **`tools/push_journals_to_sheet.py --input <path>`**
   - Loads `JOURNAL_SHEET_URL`, `JOURNAL_SHEET_TAB`, and Google creds from `.env`.
   - Validates the sheet's row 1 against the schema; writes headers if blank, errors out on mismatch.
   - Reads existing `(Company, Location, Voucher No., Particulars, Reference Invoice Number)` keys; appends only rows whose key is new.
   - Uses `RAW` value-input (per project convention) so phone-number-shaped strings and large numbers don't get coerced.
   - Prints summary JSON: `{"fetched": N, "appended": N, "skipped": N, "sheet_url": "..."}`.

## Outputs

- Appended rows in the destination Google Sheet (URL from `JOURNAL_SHEET_URL`).
- A summary printed to the terminal.
- An intermediate `.tmp/journals_<timestamp>.json` file (disposable; safe to delete).

## Edge cases

- **Tally not running / port closed** → `list_tally_companies.py` exits non-zero with a connection error. Tell the user to start Tally Prime and confirm Help → Settings → Connectivity is set to "Both" on port 9000.
- **No companies loaded** → `list_tally_companies.py` returns `[]`. Tell the user to load at least one company in Tally and stop.
- **Empty date range** → `fetch_tally_journals.py` may return 0 vouchers. Push step will still run (writes 0 rows) and the summary will say `appended: 0`.
- **`vouchers_scanned > 0` but `vouchers_with_kept_leg == 0`** — every journal in the range posts only against ledgers outside the Sundry Debtors / Branch / Divisions union (e.g. expense reclassifications, depreciation entries, vendor adjustments). Expected and correct; nothing to push.
- **Voucher posting against multiple original invoices for the same debtor** → emits one row per `BILLALLOCATIONS.LIST` entry. A ₹10L journal split across 6 invoices produces 6 rows. Dedupe key includes `Reference Invoice Number`, so re-runs stay idempotent.
- **Voucher with multiple debtor legs** → emits rows for each leg's allocations. The dedupe key includes `Particulars`, so re-runs stay idempotent.
- **Debtor leg with no `BILLALLOCATIONS.LIST`** → emits one `On Account` row with blank `ref_inv_no` and the full leg amount. This is a valid journal that simply doesn't pin to a specific original invoice.
- **`Transaction Type` empty** → only happens when the leg has no `ISDEEMEDPOSITIVE` and a zero/unparseable AMOUNT — extremely rare, indicates a malformed voucher. The row is still emitted; investigate the voucher in Tally.
- **Google OAuth token expired or missing** → `push_journals_to_sheet.py` opens the browser for re-auth on first run. If that fails non-interactively, delete `token.json` and re-run from a terminal that can open a browser.
- **Sheet header row mismatch** → `push_journals_to_sheet.py` exits non-zero. Either fix the sheet's row 1 or update `reference/columns.md`.
- **Voucher numbers reused across companies** → dedupe key is the composite `(Company, Location, Voucher No., Particulars, Reference Invoice Number)`, so this is safe.
- **Re-run with overlapping date range** → expected and supported. Push step skips already-present rows; summary will show `skipped > 0`.

## Lessons learned (carried over from sibling tally-salesdebitnote-sync-outstanding skill)

- **Don't rely on Tally's built-in `Journal Register` — enumerate voucher types via PARENT and run one Voucher Register query per type.** Voucher Register reliably honours `<VOUCHERTYPENAME>` as a filter; the built-in registers can silently filter to a single master type per company.
- **Use the older-style envelope** with `<TALLYREQUEST>Export Data</TALLYREQUEST>` and the `<EXPORTDATA><REQUESTDESC><REPORTNAME>...` wrapper. The newer `<TYPE>Data</TYPE><ID>...` form is unreliable for Voucher Register.
- **Sundry-Debtor walk is mandatory and authoritative.** Without it, journal entries against vendors, banks, expense, or other groups would pollute the receivables sheet. Even broad "Journal" voucher types are commonly used for expense reclassifications and provisioning entries.
- **Tally XML is dirty.** Raw control bytes, stray Windows-1252 bytes, and numeric character references like `&#0;` need sanitization before parsing — see `_sanitize_tally_xml` in the fetch tool.
- **Cancelled vouchers are filtered out** via `_is_cancelled` (matches `ACTION="Cancel"` and `ISCANCELLED=Yes`).
- **Company / location come from `reference/companies.md`, not Tally.** Same mapping as the sibling outstanding-report skills — keep them aligned. **Refresh every April** when the financial-year suffix in Tally rolls over.
- **Date column format is `DD/MM/YYYY`** (Indian convention, sortable as a date in Sheets). The internal `Month` column stays `YYYY-MM` because that sorts naturally as text.

## Lessons specific to this skill

- **Walk includes Branch / Divisions, not just Sundry Debtors.** Inter-branch journals are recorded with one debtor-side leg and one inter-branch leg. Without the Branch / Divisions extension, only the debtor leg would surface and the journal wouldn't visibly balance when read row-by-row. Verified 2026-04-29 against the J.P. Processors ₹10L 8-Apr inter-branch journal — Surat-side voucher emits 7 rows (1 Dr ORANGE O TEC NOIDA + 6 Cr J.P. allocations); Noida-side mirror voucher emits 2 rows (1 Dr J.P. + 1 Cr ORANGE O TEC SURAT BRANCH). Both vouchers balance row-by-row.
- **`BALANCE WITH RELATED PARTY` is intentionally NOT in the walk.** It rolls up to Sundry Creditors (a payable group). Including it would scoop in payable-side ledgers that don't belong on a receivables sheet. The `(Debtors)` sub-variant IS already covered because it rolls up to Sundry Debtors.
- **One row per `BILLALLOCATIONS.LIST` entry**, not per voucher and not per leg. A journal that posts a single ₹10L credit against six original invoices for the same debtor produces six rows — each carries that allocation's `Reference Invoice Number` and partial `Amount`. The total across rows for the voucher equals the leg amount. Same fan-out pattern as the bank-receipt and cheque-return skills.
- **Empty `<BILLALLOCATIONS.LIST/>` is treated as no allocation.** Tally emits an empty allocation list for `On Account` legs. The fetch tool detects this via `_is_real_allocation` (any of `NAME` / `BILLTYPE` / `AMOUNT` populated) and falls back to the leg-amount path.
- **`Transaction Type` is derived from `ISDEEMEDPOSITIVE` first, AMOUNT sign as fallback.** Tally's voucher XML serialises debit legs with `ISDEEMEDPOSITIVE=Yes` and AMOUNT = `-<value>`; credit legs are `ISDEEMEDPOSITIVE=No` with AMOUNT = `<value>` (no sign). Both signals usually agree, but `ISDEEMEDPOSITIVE` is more explicit. The Dr/Cr is set per-leg, so all rows from the same leg share the same direction.
- **`Amount` is always unsigned.** The sign information lives in `Transaction Type`. This matches how the sibling cheque-return skill renders the `Debit` column.
- **Voucher-level `<REFERENCE>` is intentionally not captured.** It's almost always blank for journals; the per-allocation `Reference Invoice Number` (from `BILLALLOCATIONS.LIST → NAME`) is the meaningful linkage to original invoices.
