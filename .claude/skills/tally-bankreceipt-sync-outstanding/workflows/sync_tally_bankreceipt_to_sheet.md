# Workflow: sync_tally_bankreceipt_to_sheet

## Objective

Sync receipt vouchers (Bank Receipt, Cash Receipt, JV Receipt — every voucher type whose PARENT class is `Receipt`) from local Tally Prime into the existing outstanding-report Google Sheet. Idempotent: re-running for an overlapping date range adds new bill-allocation rows only (no duplicates).

## Inputs

- **From date** (DD-MM-YYYY) — start of date range, user-supplied at runtime.
- **To date** (DD-MM-YYYY) — end of date range, user-supplied at runtime. Defaults to today if not given.
- **Companies** (optional, comma-separated) — restrict to a subset of currently loaded companies. Default: all loaded companies.
- **Project `.env`** (in the cwd) must define:
  - `TALLY_HOST` — e.g. `http://localhost:9000`
  - `GOOGLE_CREDENTIALS_FILE` — path to OAuth client secrets JSON
  - `GOOGLE_TOKEN_FILE` — path to cached OAuth token JSON
  - `BANK_RECEIPT_SHEET_URL` — full URL of the destination Google Sheet
  - `BANK_RECEIPT_SHEET_TAB` — tab name within the sheet (e.g. `Sheet1`)

## Chunking rule

**Always fetch one calendar month at a time, even if the user requests a multi-month range.** Tally's HTTP endpoint has a 180s read timeout in the fetch tool, and ranges longer than ~1 month (especially across multiple loaded companies) routinely exceed it; the failure also leaves Tally unresponsive for ~30s+, breaking subsequent calls.

For a multi-month request like `01-04-2025 → 26-04-2026`, run the fetch+push pair sequentially per month: `01-04 → 30-04`, `01-05 → 31-05`, …, `01-04-2026 → 26-04-2026` (clamp the final chunk's `to` to the user's actual end date). The push step is idempotent on `(Company, Location, Voucher No., Ref Inv No, Allocation Type)`, so per-month chunks never produce duplicates.

## Tools (in order)

All paths below are relative to the skill folder (`<SKILL_DIR>` = `.claude/skills/tally-bankreceipt-sync-outstanding/`).

1. **`tools/list_tally_companies.py`** — POSTs a Tally XML envelope to `$TALLY_HOST` requesting `List of Companies`. Prints a JSON array of company names to stdout.
   - CLI: no args.
   - Use the output to confirm with the user which companies will be included.

2. **`tools/fetch_tally_bankreceipt.py --from <DD-MM-YYYY> --to <DD-MM-YYYY> [--companies "Co1,Co2"] --output <path>`**
   - Loops over each loaded (or filtered) company, sets `SVCURRENTCOMPANY`, enumerates voucher types whose `PARENT` is in `RECEIPT_PARENT_CLASSES` (`Receipt` / `Receipts`), and pulls all vouchers of those types between `SVFROMDATE` and `SVTODATE`.
   - For each voucher, walks the party row's `BILLALLOCATIONS.LIST` and emits **one row per allocation** (matches the reference Bank Receipt.xlsx granularity). If the voucher has no bill allocations, emits a single `On Account` row.
   - Reads the column schema from `reference/columns.md` via `tools/_schema.py`.
   - Writes a JSON list-of-dicts to `<path>` (typically `.tmp/bankreceipt_<timestamp>.json`).
   - Prints a one-line summary like `{"vouchers": 312, "rows": 487, "companies": ["Enterprise","O-tec"]}`.

3. **`tools/push_bankreceipt_to_sheet.py --input <path>`**
   - Loads `BANK_RECEIPT_SHEET_URL`, `BANK_RECEIPT_SHEET_TAB`, and Google creds from `.env`.
   - Validates the sheet's row 1 against the schema; writes headers if blank, errors out on mismatch.
   - Reads existing `(Company, Location, Voucher No., Ref Inv No, Allocation Type)` keys; appends only rows whose key is new.
   - Prints summary JSON: `{"fetched": N, "appended": N, "skipped": N, "sheet_url": "..."}`.

## Outputs

- Appended rows in the destination Google Sheet (URL from `BANK_RECEIPT_SHEET_URL`).
- A summary printed to the terminal.
- An intermediate `.tmp/bankreceipt_<timestamp>.json` file (disposable; safe to delete).

## Edge cases

- **Tally not running / port closed** → `list_tally_companies.py` exits non-zero with a connection error. Tell the user to start Tally Prime and confirm Help → Settings → Connectivity is set to "Both" on port 9000.
- **No companies loaded** → `list_tally_companies.py` returns `[]`. Tell the user to load at least one company in Tally and stop.
- **Empty date range** → `fetch_tally_bankreceipt.py` may return 0 vouchers. Push step still runs (writes 0 rows) and the summary will say `appended: 0`.
- **Google OAuth token expired or missing** → `push_bankreceipt_to_sheet.py` opens the browser for re-auth on first run. If non-interactive, delete `token.json` and re-run from a terminal that can open a browser.
- **Sheet header row mismatch** → `push_bankreceipt_to_sheet.py` exits non-zero. Either fix the sheet's row 1 or update `reference/columns.md`.
- **Voucher with no bill allocations** ("On Account") → emit one row with blank `ref_inv_no`, `allocation_type = "On Account"`, `receipt_amt` = absolute party-row amount.
- **Voucher splitting one receipt across multiple bills** (e.g. ₹500k against `INK/25-26/56`, ₹200k against `INK/25-26/57`) → emit one row per `BILLALLOCATIONS.LIST` entry; each row carries that allocation's `AMOUNT`, not the voucher gross.
- **Voucher numbers reused across companies / branches** → dedupe key includes `(Company, Location)` so this is safe (Surat + Noida branches with the same display name don't collide).
- **Re-run with overlapping date range** → expected and supported. Push step skips already-present rows; summary will show `skipped > 0`.

## Lessons learned

- **Don't filter by voucher-type name** (e.g. substring `"BANK"`). Different companies in this group use different voucher-type names — including typos and abbreviations — but they all share the same parent class. Filter by `PARENT in RECEIPT_PARENT_CLASSES` (`Receipt` / `Receipts`) so all categories of receipt are captured.
- **Use the older-style XML envelope** with `<TALLYREQUEST>Export Data</TALLYREQUEST>` and the `<EXPORTDATA><REQUESTDESC><REPORTNAME>...` wrapper for `Voucher Register`. The newer `<TYPE>Data</TYPE><ID>...` form is unreliable.
- **Tally XML is dirty.** Response routinely contains raw control bytes (`0x00–0x1F`), stray Windows-1252 bytes, and numeric character references like `&#0;` that XML 1.0 forbids. `_sanitize_tally_xml` handles all three before parsing.
- **Bill allocations live on the party's `LEDGERENTRIES.LIST` row, not the voucher root.** A receipt voucher has at least two ledger entries (party + bank/cash). The party row carries the `BILLALLOCATIONS.LIST` children; iterate those for the bill-wise breakdown. Match by `LEDGERNAME == PARTYLEDGERNAME` (with a max-abs-amount fallback if names don't line up).
- **Receipt amounts are stored as negatives in Tally** (the bank/cash debit; the party credit). The reference Excel always shows positives — strip the sign in `_amount_abs`.
- **Granularity is bill-allocation, not voucher.** One Tally receipt voucher → N rows in the sheet (one per `BILLALLOCATIONS.LIST` entry). This matches the reference Excel where the same date/customer appears multiple times against different `Ref Inv No` values.
- **`Ref Inv No` is the original sales voucher number being settled** — *not* the receipt's own voucher number. Tally returns it as the `<NAME>` element inside `BILLALLOCATIONS.LIST`. The receipt voucher's own number lives in the separate `Voucher No.` column (added for dedupe; not in the original reference Excel).
- **Allocation types observed:** `Agst Ref` (against an existing bill), `Advance` (advance against a future bill — still has a Ref Inv No), `On Account` (unallocated; blank Ref Inv No), `New Ref` (creating a new bill ref). Pass `BILLTYPE` through verbatim; default to `On Account` if missing.
- **Cancelled vouchers** are empty placeholders (`ACTION="Cancel"` or `ISCANCELLED=Yes`). Filter them out so they don't pollute the sheet with blank rows.
- **Company / location come from `reference/companies.md`, not Tally.** Same mapping file as `tally-sales-sync-outstanding` — keep them in sync. Refresh every April when the FY suffix in the Tally name rolls over.
- **In-batch dedupe matters too.** A single fetch run can produce two identical (Company, Location, Voucher No., Ref Inv No, Allocation Type) rows if Tally returns the same voucher under two different voucher-type queries (rare, but possible if a type is re-classified mid-run). The push tool now adds each new key into `seen` before processing the next row, so in-batch duplicates are skipped too.
