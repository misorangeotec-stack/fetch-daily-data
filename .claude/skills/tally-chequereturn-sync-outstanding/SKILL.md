---
name: tally-chequereturn-sync-outstanding
description: Sync cheque-return entries (Bank Payment vouchers booked against Sundry Debtors — i.e. dishonored customer cheques) from local Tally Prime into the Chq Return Entries Google Sheet. Use whenever the user asks to fetch/pull/sync/export Tally cheque returns, dishonored cheque entries, or bank payments against debtors for the outstanding report, update the Chq Return sheet, or run the daily cheque-return sync. Handles multiple loaded companies, user-supplied date range, filters Bank Payment vouchers to Sundry-Debtor parties only, captures the Reference Invoice Number from BILLALLOCATIONS.LIST, and dedupes by (Company, Location, Voucher No., Reference Invoice Number).
---

# tally-chequereturn-sync-outstanding

Pulls Bank Payment vouchers from local Tally Prime via XML over HTTP, **filters to vouchers whose party ledger sits under the `Sundry Debtors` group** (the bank-payment-against-debtor pattern that records a dishonored / returned cheque), walks each voucher's `BILLALLOCATIONS.LIST` to produce one row per bill allocation, then appends them (deduped) into the Chq Return Entries Google Sheet.

This skill is **self-contained**: tools and workflow live inside this folder. It expects to be run from the `FETCH DAILY DATA` project so it can pick up the project's `.env` (Tally host, Google credentials, sheet URL).

## Why "bank payment against a debtor" = cheque return

Customer cheques received are first booked as Bank Receipts (Bank Dr / Debtor Cr). When the bank later returns/dishonors the cheque, the reversal posts as a Bank Payment against that same debtor (Debtor Dr / Bank Cr). So the universe of "cheque returns" inside Tally = Bank Payment vouchers where the party ledger's parent group is `Sundry Debtors`. This skill applies exactly that filter; it does not rely on any free-text narration or voucher-class flag.

## Steps

Follow the SOP in [`workflows/sync_tally_chqreturn_to_sheet.md`](workflows/sync_tally_chqreturn_to_sheet.md). At a high level:

1. **Verify env.** Confirm the project `.env` exists and has these keys: `TALLY_HOST`, `GOOGLE_CREDENTIALS_FILE`, `GOOGLE_TOKEN_FILE`, `CHQ_RETURN_SHEET_URL`, `CHQ_RETURN_SHEET_TAB`. If any are missing, prompt the user before running anything.

2. **List loaded companies.** Run:
   ```
   python "<SKILL_DIR>/tools/list_tally_companies.py"
   ```
   Show the user the returned companies. If none, tell the user to load companies in Tally and stop.

3. **Ask for date range.** Prompt the user for `from` and `to` dates in `DD-MM-YYYY` format. Default `to` to today if the user only gives `from`.

4. **Fetch cheque returns.** Run:
   ```
   python "<SKILL_DIR>/tools/fetch_tally_chqreturn.py" --from <DD-MM-YYYY> --to <DD-MM-YYYY> --output .tmp/chqreturn_<timestamp>.json
   ```
   (Add `--companies "Co1,Co2"` if the user wants a subset.) The script reads the column schema from this skill's [`reference/columns.md`](reference/columns.md), so you do not need to know the schema.

5. **Push to sheet.** Run:
   ```
   python "<SKILL_DIR>/tools/push_chqreturn_to_sheet.py" --input .tmp/chqreturn_<timestamp>.json
   ```
   The script appends only rows whose `(Company, Location, Vch No., Reference Invoice Number)` aren't already in the sheet. It validates the sheet's header row against `reference/columns.md` first.

6. **Report.** Read the JSON summary printed by the push script and tell the user: rows fetched, rows appended, rows skipped (duplicates), and the sheet URL.

## Schema changes

The output schema (column names, order, Tally source mapping) lives in [`reference/columns.md`](reference/columns.md). To add/remove/reorder a column, edit that table — no Python changes needed. The shared loader [`tools/_schema.py`](tools/_schema.py) parses it.

## Troubleshooting

- **`Connection refused` to Tally** — Tally Prime isn't running, or it's not exposing port 9000. In Tally: `F1 (Help) → Settings → Connectivity → Client/Server configuration → TallyPrime acting as: Both`.
- **Google auth error** — delete `token.json` (path from `GOOGLE_TOKEN_FILE`) and re-run; the push script will re-open the OAuth browser flow.
- **`Sheet header mismatch`** — the destination tab has a non-empty row 1 that doesn't match `reference/columns.md`. Either fix the sheet's header row to match, or update the schema file.
- **No rows fetched even though Bank Payments exist** — the Sundry-Debtor filter is doing its job: bank payments to vendors / expense ledgers are excluded by design. Only payments whose party ledger sits under group `Sundry Debtors` qualify as cheque returns. Verify the party's ledger group in Tally if you expected a row to show up.
- **Duplicate rows after a run** — confirm the dedupe key is still `(company, location, voucher_no, ref_inv_no)` in `push_chqreturn_to_sheet.py`. If `reference/columns.md` keys were renamed, the script needs the new key names.
