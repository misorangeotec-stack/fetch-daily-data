---
name: tally-salesdebitnote-sync-outstanding
description: Sync sales debit-note vouchers (every voucher type with PARENT class "Debit Note", filtered to vouchers booked against a Sundry Debtor party — sales-side debits like late fees, interest, freight recovery, rate-difference debits, 194R FOC, TCS debit notes, GST debit notes including ISD, SALES DEBIT NOTE) from local Tally Prime into the outstanding-report Google Sheet. Use whenever the user asks to fetch/pull/sync/export Tally sales debit notes or debit notes for the outstanding report, update the outstanding debit-note sheet, run the daily debit-note sync, or push Tally debit-note data to the outstanding sheet. Handles multiple loaded companies, user-supplied date range, captures the original sales invoice via voucher REFERENCE, captures voucher narration, walks the party ledger's group chain to keep only Sundry-Debtor parties, and dedupes by (Company, Location, Voucher No.).
---

# tally-salesdebitnote-sync-outstanding

Pulls **debit-note vouchers booked against Sundry Debtors** from local Tally Prime via XML over HTTP, then appends them (deduped) into a single Google Sheet that feeds the outstanding (receivables) report.

This is the sales-side counterpart of the `tally-salescreditnote-sync-outstanding` skill. Debit notes against debtors are typically raised for late-payment interest, freight/short-billing recovery, rate-difference debits, 194R FOC charges, TCS debit notes, ISD reversals charged through to a debtor, and similar receivable-increasing adjustments. Purchase-side debit notes (PURCHASE DEBIT NOTE, PURCHASE RETURN) are skipped by name; everything else under the `Debit Note` parent flows through and is then filtered authoritatively by the party-side Sundry Debtor walk.

This skill is **self-contained**: tools and workflow live inside this folder. It expects to be run from the `FETCH DAILY DATA` project so it can pick up the project's `.env` (Tally host, Google credentials, sheet URL).

## Steps

Follow the SOP in [`workflows/sync_tally_debit_notes_to_sheet.md`](workflows/sync_tally_debit_notes_to_sheet.md). At a high level:

1. **Verify env.** Confirm the project `.env` exists and has these keys: `TALLY_HOST`, `GOOGLE_CREDENTIALS_FILE`, `GOOGLE_TOKEN_FILE`, `DEBIT_NOTE_SHEET_URL`, `DEBIT_NOTE_SHEET_TAB`. If any are missing, prompt the user before running anything.

2. **List loaded companies.** Run:
   ```
   python "<SKILL_DIR>/tools/list_tally_companies.py"
   ```
   Show the user the returned companies. If none, tell the user to load companies in Tally and stop.

3. **Ask for date range.** Prompt the user for `from` and `to` dates in `DD-MM-YYYY` format. Default `to` to today if the user only gives `from`.

4. **Fetch debit notes.** Run:
   ```
   python "<SKILL_DIR>/tools/fetch_tally_debit_notes.py" --from <DD-MM-YYYY> --to <DD-MM-YYYY> --output .tmp/debit_notes_<timestamp>.json
   ```
   (Add `--companies "Co1,Co2"` if the user wants a subset.) The script reads the column schema from this skill's [`reference/columns.md`](reference/columns.md), so you do not need to know the schema.

5. **Push to sheet.** Run:
   ```
   python "<SKILL_DIR>/tools/push_debit_notes_to_sheet.py" --input .tmp/debit_notes_<timestamp>.json
   ```
   The script appends only rows whose `(Company, Location, Voucher No.)` aren't already in the sheet. It validates the sheet's header row against `reference/columns.md` first; if the row is blank it writes the header.

6. **Report.** Read the JSON summary printed by the push script and tell the user: rows fetched, rows appended, rows skipped (duplicates), and the sheet URL.

## Filter pipeline (two-stage)

This skill uses both a coarse name filter and an authoritative party filter:

1. **Voucher-type filter.** Include voucher types whose PARENT class is `Debit Note` and whose name does **not** contain `purchase`, `purch`, or `return`. This drops `PURCHASE DEBIT NOTE` and `PURCHASE RETURN` early without a Tally roundtrip per voucher. `GST DEBIT NOTE-ISD` is **kept** at this stage — if it's booked against a creditor it's dropped by the party filter; if booked against a debtor it's emitted (rare ISD reversal pass-through).
2. **Party Sundry-Debtor filter.** For each voucher kept by stage 1, walk the party ledger's group chain (using the Tally Group master) and keep only vouchers whose party rolls up to the primary group `Sundry Debtors`. This is the authoritative filter — even if a user mis-categorises a voucher type, only debtor-party vouchers are emitted.

The Sundry-Debtor walk is the same pattern used by the sibling `tally-chequereturn-sync-outstanding` skill.

## Schema changes

The output schema (column names, order, Tally source mapping) lives in [`reference/columns.md`](reference/columns.md). To add/remove/reorder a column, edit that table — no Python changes needed. The shared loader [`tools/_schema.py`](tools/_schema.py) parses it.

## Troubleshooting

- **`Connection refused` to Tally** — Tally Prime isn't running, or it's not exposing port 9000. In Tally: `F1 (Help) → Settings → Connectivity → Client/Server configuration → TallyPrime acting as: Both`.
- **Google auth error** — delete `token.json` (path from `GOOGLE_TOKEN_FILE`) and re-run; the push script will re-open the OAuth browser flow.
- **`Sheet header mismatch`** — the destination tab has a non-empty row 1 that doesn't match `reference/columns.md`. Either fix the sheet's header row to match, or update the schema file.
- **0 rows even though Tally has debit notes** — the party filter dropped them as non-debtors. Either the voucher's party is a Sundry Creditor (correctly skipped — purchase-side) or the party ledger sits under a non-standard group not rolling up to `Sundry Debtors`. Check the ledger's primary group in Tally.
- **Duplicate rows after a run** — confirm the dedupe key is still `(Company, Location, Voucher No.)` in `push_debit_notes_to_sheet.py`. If `reference/columns.md` keys were renamed, the script needs the new key names.
- **`Against Sales Invoice no.` is blank when it shouldn't be** — the field comes from the voucher-level `<REFERENCE>` tag. Some debit-note voucher types (interest, late-fee) never reference a prior invoice, so blank is expected. If the user wants bill-allocation fallback, edit `voucher_to_row()` in `fetch_tally_debit_notes.py`.
