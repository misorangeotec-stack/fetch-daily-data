---
name: tally-salescreditnote-sync-outstanding
description: Sync sales credit-note vouchers (every voucher type with PARENT class "Credit Note" — GST CREDIT NOTE, GST SALES RETURN, etc.) from local Tally Prime into the outstanding-report Google Sheet. Use whenever the user asks to fetch/pull/sync/export Tally sales credit notes or credit notes for the outstanding report, update the outstanding credit-note sheet, run the daily credit-note sync, or push Tally credit-note data to the outstanding sheet. Handles multiple loaded companies, user-supplied date range, captures the original sales invoice via voucher REFERENCE, and dedupes by (Company, Location, Voucher No.).
---

# tally-salescreditnote-sync-outstanding

Pulls sales credit-note vouchers from local Tally Prime via XML over HTTP, then appends them (deduped) into a single Google Sheet that feeds the outstanding (receivables) report.

This skill is **self-contained**: tools and workflow live inside this folder. It expects to be run from the `FETCH DAILY DATA` project so it can pick up the project's `.env` (Tally host, Google credentials, sheet URL).

## Steps

Follow the SOP in [`workflows/sync_tally_credit_notes_to_sheet.md`](workflows/sync_tally_credit_notes_to_sheet.md). At a high level:

1. **Verify env.** Confirm the project `.env` exists and has these keys: `TALLY_HOST`, `GOOGLE_CREDENTIALS_FILE`, `GOOGLE_TOKEN_FILE`, `CREDIT_NOTE_SHEET_URL`, `CREDIT_NOTE_SHEET_TAB`. If any are missing, prompt the user before running anything.

2. **List loaded companies.** Run:
   ```
   python "<SKILL_DIR>/tools/list_tally_companies.py"
   ```
   Show the user the returned companies. If none, tell the user to load companies in Tally and stop.

3. **Ask for date range.** Prompt the user for `from` and `to` dates in `DD-MM-YYYY` format. Default `to` to today if the user only gives `from`.

4. **Fetch credit notes.** Run:
   ```
   python "<SKILL_DIR>/tools/fetch_tally_credit_notes.py" --from <DD-MM-YYYY> --to <DD-MM-YYYY> --output .tmp/credit_notes_<timestamp>.json
   ```
   (Add `--companies "Co1,Co2"` if the user wants a subset.) The script reads the column schema from this skill's [`reference/columns.md`](reference/columns.md), so you do not need to know the schema.

5. **Push to sheet.** Run:
   ```
   python "<SKILL_DIR>/tools/push_credit_notes_to_sheet.py" --input .tmp/credit_notes_<timestamp>.json
   ```
   The script appends only rows whose `(Company, Location, Voucher No.)` aren't already in the sheet. It validates the sheet's header row against `reference/columns.md` first.

6. **Report.** Read the JSON summary printed by the push script and tell the user: rows fetched, rows appended, rows skipped (duplicates), and the sheet URL.

## Schema changes

The output schema (column names, order, Tally source mapping) lives in [`reference/columns.md`](reference/columns.md). To add/remove/reorder a column, edit that table — no Python changes needed. The shared loader [`tools/_schema.py`](tools/_schema.py) parses it.

## Troubleshooting

- **`Connection refused` to Tally** — Tally Prime isn't running, or it's not exposing port 9000. In Tally: `F1 (Help) → Settings → Connectivity → Client/Server configuration → TallyPrime acting as: Both`.
- **Google auth error** — delete `token.json` (path from `GOOGLE_TOKEN_FILE`) and re-run; the push script will re-open the OAuth browser flow.
- **`Sheet header mismatch`** — the destination tab has a non-empty row 1 that doesn't match `reference/columns.md`. Either fix the sheet's header row to match, or update the schema file.
- **Duplicate rows after a run** — confirm the dedupe key is still `(Company, Location, Voucher No.)` in `push_credit_notes_to_sheet.py`. If `reference/columns.md` keys were renamed, the script needs the new key names.
- **`Against Sales Invoice no.` is blank when it shouldn't be** — the field comes from the voucher-level `<REFERENCE>` tag. Some users leave this blank in Tally even when there's an underlying sales invoice (the bill linkage shows up only inside `LEDGERENTRIES.LIST/BILLALLOCATIONS.LIST/NAME`). The reference Excel uses `<REFERENCE>` exclusively, so this skill matches that convention. If the user wants bill-allocation fallback, edit `voucher_to_row()` in `fetch_tally_credit_notes.py`.
