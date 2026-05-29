# Workflow: sync_tally_chqreturn_to_sheet

## Objective

Sync cheque-return entries (Bank Payment vouchers whose party ledger sits under the Sundry Debtors group — i.e. dishonored customer cheques being reversed) from local Tally Prime into the Chq Return Entries Google Sheet. Idempotent: re-running for an overlapping date range adds new bill-allocation rows only (no duplicates).

## Inputs

- **From date** (DD-MM-YYYY) — start of date range, user-supplied at runtime.
- **To date** (DD-MM-YYYY) — end of date range, user-supplied at runtime. Defaults to today if not given.
- **Companies** (optional, comma-separated) — restrict to a subset of currently loaded companies. Default: all loaded companies.
- **Project `.env`** (in the cwd) must define:
  - `TALLY_HOST` — e.g. `http://localhost:9000`
  - `GOOGLE_CREDENTIALS_FILE` — path to OAuth client secrets JSON
  - `GOOGLE_TOKEN_FILE` — path to cached OAuth token JSON
  - `CHQ_RETURN_SHEET_URL` — full URL of the destination Google Sheet
  - `CHQ_RETURN_SHEET_TAB` — tab name within the sheet (default: `Sheet1`)

## Chunking rule

**Always fetch one calendar month at a time, even if the user requests a multi-month range.** Tally's HTTP endpoint has a 180s read timeout in the fetch tool, and ranges longer than ~1 month (especially across multiple loaded companies) routinely exceed it; the failure also leaves Tally unresponsive for ~30s+, breaking subsequent calls.

For a multi-month request, run the fetch+push pair sequentially per month: `01-MM → end-of-month`, etc. (clamp the final chunk's `to` to the user's actual end date). The push step is idempotent on `(Company, Location, Voucher No., Reference Invoice Number)`, so per-month chunks never produce duplicates.

## Tools (in order)

All paths below are relative to the skill folder (`<SKILL_DIR>` = `.claude/skills/tally-chequereturn-sync-outstanding/`).

1. **`tools/list_tally_companies.py`** — POSTs a Tally XML envelope to `$TALLY_HOST` requesting `List of Companies`. Prints a JSON array of company names to stdout.
   - CLI: no args.
   - Use the output to confirm with the user which companies will be included.

2. **`tools/fetch_tally_chqreturn.py --from <DD-MM-YYYY> --to <DD-MM-YYYY> [--companies "Co1,Co2"] --output <path>`**
   - For each loaded (or filtered) company, sets `SVCURRENTCOMPANY` and runs three queries:
     1. **Group master** (`Collection TYPE=Group, FETCH NAME,PARENT`) — used to walk the parent chain and identify every group that resolves up to the primary `Sundry Debtors` group (handles user-defined sub-groups).
     2. **Ledger master** (`Collection TYPE=Ledger, FETCH NAME,PARENT`) — used to build the per-company set of debtor ledgers.
     3. **Voucher Register** for each voucher type whose `PARENT in {Payment, Payments}` AND whose name contains "BANK" (case-insensitive). This restricts to Bank Payments and excludes Cash / JV / Purchase payments.
   - For each fetched voucher, drops the row if the voucher's `PARTYLEDGERNAME` (or `PARTYNAME` fallback) is **not** a Sundry Debtor.
   - For each surviving voucher, walks the party row's `BILLALLOCATIONS.LIST` and emits **one row per allocation**. The allocation's `<NAME>` becomes `Reference Invoice Number`, the `<AMOUNT>` becomes `Debit` (positive). If the voucher has no bill allocations, emits a single `On Account` row with blank Ref Inv No.
   - Reads the column schema from `reference/columns.md` via `tools/_schema.py`.
   - Writes a JSON list-of-dicts to `<path>` (typically `.tmp/chqreturn_<timestamp>.json`).
   - Prints a one-line summary like `{"vouchers_scanned": 412, "vouchers_against_debtors": 18, "rows": 24, "companies": ["Enterprise","O-tec"]}`.

3. **`tools/push_chqreturn_to_sheet.py --input <path>`**
   - Loads `CHQ_RETURN_SHEET_URL`, `CHQ_RETURN_SHEET_TAB`, and Google creds from `.env`.
   - Validates the sheet's row 1 against the schema; writes headers if blank, errors out on mismatch.
   - Reads existing `(Company, Location, Vch No., Reference Invoice Number)` keys; appends only rows whose key is new.
   - Prints summary JSON: `{"fetched": N, "appended": N, "skipped": N, "sheet_url": "..."}`.

## Outputs

- Appended rows in the destination Google Sheet (URL from `CHQ_RETURN_SHEET_URL`).
- A summary printed to the terminal.
- An intermediate `.tmp/chqreturn_<timestamp>.json` file (disposable; safe to delete).

## Edge cases

- **Tally not running / port closed** → `list_tally_companies.py` exits non-zero with a connection error. Tell the user to start Tally Prime and confirm Help → Settings → Connectivity is set to "Both" on port 9000.
- **No companies loaded** → `list_tally_companies.py` returns `[]`. Tell the user to load at least one company in Tally and stop.
- **Empty date range** → `fetch_tally_chqreturn.py` may return 0 vouchers. Push step still runs (writes 0 rows) and the summary will say `appended: 0`.
- **Many Bank Payments scanned but few rows kept** → expected. Most bank payments are to vendors/expense ledgers; only the small subset booked against debtors qualifies as a cheque return. `vouchers_against_debtors` in the summary tells you the kept count.
- **Google OAuth token expired or missing** → `push_chqreturn_to_sheet.py` opens the browser for re-auth on first run. If non-interactive, delete `token.json` and re-run from a terminal that can open a browser.
- **Sheet header row mismatch** → `push_chqreturn_to_sheet.py` exits non-zero. Either fix the sheet's row 1 or update `reference/columns.md`.
- **Voucher with no bill allocations** ("On Account") → emit one row with blank `ref_inv_no`, `debit` = absolute party-row amount.
- **Voucher splitting one bounced cheque across multiple original invoices** → emit one row per `BILLALLOCATIONS.LIST` entry; each row carries that allocation's `AMOUNT`, not the voucher gross.
- **Voucher numbers reused across companies / branches** → dedupe key includes `(Company, Location)` so this is safe (Surat + Noida branches with the same display name don't collide).
- **Re-run with overlapping date range** → expected and supported. Push step skips already-present rows; summary will show `skipped > 0`.

## Lessons learned

- **Cheque returns are not their own voucher type.** Tally records a dishonored cheque as a Bank Payment whose debit goes to the same Sundry Debtor as the original Bank Receipt. There is no `BILLTYPE = "Returned"` flag, no cheque-return narration convention, and no separate voucher class. The only reliable filter is "Bank Payment voucher AND party is a Sundry Debtor."
- **Filter voucher types by parent + name, not name alone.** Different companies in this group use different voucher-type names ("BANK PAYMENT", "Bank Payment", possibly with typos). Filtering on `PARENT in {Payment, Payments}` AND `name contains "BANK"` catches all variants while excluding Cash / JV / Purchase payments.
- **Walk the group hierarchy to identify debtors.** Tally's reserved primary group is `Sundry Debtors`, but users routinely create sub-groups like "Sundry Debtors - Domestic" or "Sundry Debtors - Export". Resolving each ledger's parent chain up to a primary group catches all of them in one pass.
- **Use the older-style XML envelope** with `<TALLYREQUEST>Export Data</TALLYREQUEST>` and the `<EXPORTDATA><REQUESTDESC><REPORTNAME>...` wrapper for `Voucher Register`. The newer `<TYPE>Data</TYPE><ID>...` form is unreliable.
- **Tally XML is dirty.** Response routinely contains raw control bytes (`0x00–0x1F`), stray Windows-1252 bytes, and numeric character references like `&#0;` that XML 1.0 forbids. `_sanitize_tally_xml` handles all three before parsing.
- **Bill allocations live on the party's `LEDGERENTRIES.LIST` row, not the voucher root.** A bank payment voucher has at least two ledger entries (party + bank). The party row carries the `BILLALLOCATIONS.LIST` children; iterate those for the bill-wise breakdown. Match by `LEDGERNAME == PARTYLEDGERNAME` (with a max-abs-amount fallback if names don't line up).
- **Bank-payment-against-debtor amounts are stored positive on the party row** (debit) — opposite sign to receipt vouchers. We still pass through `_amount_abs` to match the reference Excel which always shows positives.
- **Granularity is bill-allocation, not voucher.** One bounced cheque originally settling two invoices → two rows in the sheet, each carrying the original invoice in `Reference Invoice Number` and that bill's portion of the cheque in `Debit`. This matches the bankreceipt skill's pattern.
- **`Reference Invoice Number` is the original sales voucher whose cheque bounced** — `<NAME>` element inside `BILLALLOCATIONS.LIST`. Not the bank-payment voucher's own number, which lives in `Vch No.`. The reference Excel did not originally include this column; it was added at user request because it makes the row joinable back to the sales sheet.
- **Cancelled vouchers** (`ACTION="Cancel"` or `ISCANCELLED=Yes`) are filtered out — they're empty placeholders that would pollute the sheet with blank rows.
- **Company / location come from `reference/companies.md`, not Tally.** Same mapping file shape as the sibling skills — keep them in sync. Refresh every April when the FY suffix in the Tally name rolls over.
- **In-batch dedupe matters too.** A single fetch run can produce duplicate keys if Tally returns the same voucher under two different voucher-type queries (rare). The push tool adds each new key into `seen` before processing the next row, so in-batch duplicates are skipped too.
