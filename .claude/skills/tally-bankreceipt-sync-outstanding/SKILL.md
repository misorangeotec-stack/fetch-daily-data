---
name: tally-bankreceipt-sync-outstanding
description: Sync receipt vouchers (Bank Receipt, Cash Receipt, JV Receipt — every voucher type with PARENT class "Receipt") from local Tally Prime into the outstanding-report Google Sheet. Use whenever the user asks to fetch/pull/sync/export Tally bank receipts or receipts for the outstanding report, update the outstanding bank-receipt sheet, run the daily bank-receipt sync, or push Tally receipt data to the outstanding sheet. Handles multiple loaded companies, user-supplied date range, and emits one row per bill allocation (deduped).
---

# tally-bankreceipt-sync-outstanding

Pulls receipt vouchers from local Tally Prime via XML over HTTP, walks each voucher's `BILLALLOCATIONS.LIST` to produce one row per bill allocation, then appends them (deduped) into a single Google Sheet that feeds the outstanding (receivables) report.

This skill is **self-contained**: tools and workflow live inside this folder. It expects to be run from the `FETCH DAILY DATA` project so it can pick up the project's `.env` (Tally host, Google credentials, sheet URL).

## Steps

Follow the SOP in [`workflows/sync_tally_bankreceipt_to_sheet.md`](workflows/sync_tally_bankreceipt_to_sheet.md). At a high level:

1. **Verify env.** Confirm the project `.env` exists and has these keys: `TALLY_HOST`, `GOOGLE_CREDENTIALS_FILE`, `GOOGLE_TOKEN_FILE`, `BANK_RECEIPT_SHEET_URL`, `BANK_RECEIPT_SHEET_TAB`. If any are missing, prompt the user before running anything.

2. **List loaded companies.** Run:
   ```
   python "<SKILL_DIR>/tools/list_tally_companies.py"
   ```
   Show the user the returned companies. If none, tell the user to load companies in Tally and stop.

3. **Ask for date range.** Prompt the user for `from` and `to` dates in `DD-MM-YYYY` format. Default `to` to today if the user only gives `from`.

4. **Fetch receipts.** Run:
   ```
   python "<SKILL_DIR>/tools/fetch_tally_bankreceipt.py" --from <DD-MM-YYYY> --to <DD-MM-YYYY> --output .tmp/bankreceipt_<timestamp>.json
   ```
   (Add `--companies "Co1,Co2"` if the user wants a subset.) The script reads the column schema from this skill's [`reference/columns.md`](reference/columns.md), so you do not need to know the schema.

5. **Push to sheet.** Run:
   ```
   python "<SKILL_DIR>/tools/push_bankreceipt_to_sheet.py" --input .tmp/bankreceipt_<timestamp>.json
   ```
   The script appends only rows whose `(Company, Location, Voucher No., Ref Inv No, Allocation Type)` aren't already in the sheet. It validates the sheet's header row against `reference/columns.md` first.

6. **Report.** Read the JSON summary printed by the push script and tell the user: rows fetched, rows appended, rows skipped (duplicates), and the sheet URL.

## Schema changes

The output schema (column names, order, Tally source mapping) lives in [`reference/columns.md`](reference/columns.md). To add/remove/reorder a column, edit that table — no Python changes needed. The shared loader [`tools/_schema.py`](tools/_schema.py) parses it.

## Troubleshooting

- **`Connection refused` to Tally** — Tally Prime isn't running, or it's not exposing port 9000. In Tally: `F1 (Help) → Settings → Connectivity → Client/Server configuration → TallyPrime acting as: Both`.
- **Google auth error** — delete `token.json` (path from `GOOGLE_TOKEN_FILE`) and re-run; the push script will re-open the OAuth browser flow.
- **`Sheet header mismatch`** — the destination tab has a non-empty row 1 that doesn't match `reference/columns.md`. Either fix the sheet's header row to match, or update the schema file.
- **Duplicate rows after a run** — confirm the dedupe key is still `(company, location, voucher_no, ref_inv_no, allocation_type)` in `push_bankreceipt_to_sheet.py`. If `reference/columns.md` keys were renamed, the script needs the new key names.
- **No "On Account" rows showing** — the fetcher only emits `On Account` when a voucher has *zero* bill allocations on the party row. If Tally is returning `BILLTYPE = "On Account"` inside `BILLALLOCATIONS.LIST` instead, that's passed through as-is.
