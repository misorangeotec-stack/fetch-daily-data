---
name: tally-salesjournal-sync-outstanding
description: Sync journal vouchers touching Sundry Debtors or Branch/Divisions ledgers (every voucher type with PARENT class "Journal" — debtor-side reclassifications, provisioning entries, write-offs, late-payment interest journals, inter-branch settlements, manual receivable adjustments) from local Tally Prime into the outstanding-report Google Sheet. Use whenever the user asks to fetch/pull/sync/export Tally journal entries or journals for the outstanding report, update the outstanding journal sheet, run the daily journal sync, or push Tally journal data to the outstanding sheet. Handles multiple loaded companies, user-supplied date range, walks each ledger's group chain to keep legs that roll up to either Sundry Debtors or Branch / Divisions, emits one row per BILLALLOCATIONS entry (so a ₹10L journal split across six original invoices yields six rows) with a Transaction Type column (`Dr`/`Cr`) and Reference Invoice Number from BILLALLOCATIONS.LIST → NAME, captures voucher narration, and dedupes by (Company, Location, Voucher No., Particulars, Reference Invoice Number).
---

# tally-salesjournal-sync-outstanding

Pulls **journal vouchers booked against Sundry Debtors** from local Tally Prime via XML over HTTP, then appends them (deduped) into a single Google Sheet that feeds the outstanding (receivables) report.

This sits in the same family as the existing `tally-salescreditnote-sync-outstanding` and `tally-salesdebitnote-sync-outstanding` skills. Where those handle dedicated credit-note / debit-note vouchers, this one handles **manual journal entries** that move money in or out of a debtor ledger — late-payment interest journals, provisioning entries, write-offs, balance reclassifications, and similar receivable-touching adjustments. Each row is tagged with a `Transaction Type` of `Dr` (receivable up) or `Cr` (receivable down) so the outstanding report can sum them correctly.

**Row granularity is per-allocation, not per-voucher.** A journal that posts a single ₹10L credit against six original invoices for the same debtor produces six rows — each carries that allocation's Reference Invoice Number and partial amount. Same fan-out pattern as the bank-receipt and cheque-return skills.

This skill is **self-contained**: tools and workflow live inside this folder. It expects to be run from the `FETCH DAILY DATA` project so it can pick up the project's `.env` (Tally host, Google credentials, sheet URL).

## Steps

Follow the SOP in [`workflows/sync_tally_journals_to_sheet.md`](workflows/sync_tally_journals_to_sheet.md). At a high level:

1. **Verify env.** Confirm the project `.env` exists and has these keys: `TALLY_HOST`, `GOOGLE_CREDENTIALS_FILE`, `GOOGLE_TOKEN_FILE`, `JOURNAL_SHEET_URL`, `JOURNAL_SHEET_TAB`. If any are missing, prompt the user before running anything.

2. **List loaded companies.** Run:
   ```
   python "<SKILL_DIR>/tools/list_tally_companies.py"
   ```
   Show the user the returned companies. If none, tell the user to load companies in Tally and stop.

3. **Ask for date range.** Prompt the user for `from` and `to` dates in `DD-MM-YYYY` format. Default `to` to today if the user only gives `from`.

4. **Fetch journals.** Run:
   ```
   python "<SKILL_DIR>/tools/fetch_tally_journals.py" --from <DD-MM-YYYY> --to <DD-MM-YYYY> --output .tmp/journals_<timestamp>.json
   ```
   (Add `--companies "Co1,Co2"` if the user wants a subset.) The script reads the column schema from this skill's [`reference/columns.md`](reference/columns.md), so you do not need to know the schema.

5. **Push to sheet.** Run:
   ```
   python "<SKILL_DIR>/tools/push_journals_to_sheet.py" --input .tmp/journals_<timestamp>.json
   ```
   The script appends only rows whose `(Company, Location, Voucher No., Particulars)` aren't already in the sheet. It validates the sheet's header row against `reference/columns.md` first; if the row is blank it writes the header.

6. **Report.** Read the JSON summary printed by the push script and tell the user: rows fetched, rows appended, rows skipped (duplicates), and the sheet URL.

## Filter pipeline (two-stage)

This skill applies one coarse filter and one authoritative filter:

1. **Voucher-type filter.** Include voucher types whose PARENT class is `Journal` (or `Journals`). No name pre-filter — journal voucher types vary widely by site and the party-leg filter below is the authoritative cut.
2. **Target-leg filter (Sundry Debtors ∪ Branch / Divisions).** For each voucher kept by stage 1, walk the company's GROUP master to find every group rolling up to either of these primary groups: `Sundry Debtors` or `Branch / Divisions`. Then walk the LEDGER master to collect every ledger sitting under one of those groups. For each voucher's `LEDGERENTRIES.LIST` legs, **keep only the legs whose ledger is in that target set**. Then for each kept leg, emit **one row per `BILLALLOCATIONS.LIST` entry** under it (carrying the allocation's `NAME` as the Reference Invoice Number and the allocation's `AMOUNT`). Legs with no allocations emit one `On Account` row with blank `ref_inv_no` and the full leg amount. Vouchers with no kept leg are dropped entirely.

   **Why include Branch / Divisions?** Inter-branch journals like `Dr ORANGE O TEC NOIDA / Cr J.P. PROCESSORS ₹10L` are recorded with one debtor-side leg and one inter-branch leg. Without the Branch / Divisions extension, only the debtor leg would surface and the journal wouldn't visibly balance row-by-row. Including the Branch / Divisions leg restores the Dr/Cr symmetry. `BALANCE WITH RELATED PARTY` (without `(Debtors)`) is intentionally excluded — it rolls up to Sundry Creditors and doesn't belong on a receivables sheet.

The Sundry-Debtor walk is the same pattern used by `tally-chequereturn-sync-outstanding` and `tally-salesdebitnote-sync-outstanding`.

## Transaction Type (Dr / Cr)

Each row's `Transaction Type` reflects which side the debtor leg sits on:

- `Dr` — receivable increased (party debited). Common for late-payment interest journals, balance reclassifications onto the debtor.
- `Cr` — receivable reduced (party credited). Common for write-offs, provisioning, and balance moves off the debtor.

The script reads `ISDEEMEDPOSITIVE` first (`Yes` → `Dr`, `No` → `Cr`) and falls back to AMOUNT sign (`-` prefix → `Dr`, no prefix → `Cr`) if the flag is missing. `Amount` is always the unsigned absolute value — the sign information lives in `Transaction Type`.

## Schema changes

The output schema (column names, order, Tally source mapping) lives in [`reference/columns.md`](reference/columns.md). To add/remove/reorder a column, edit that table — no Python changes needed. The shared loader [`tools/_schema.py`](tools/_schema.py) parses it.

## Troubleshooting

- **`Connection refused` to Tally** — Tally Prime isn't running, or it's not exposing port 9000. In Tally: `F1 (Help) → Settings → Connectivity → Client/Server configuration → TallyPrime acting as: Both`.
- **Google auth error** — delete `token.json` (path from `GOOGLE_TOKEN_FILE`) and re-run; the push script will re-open the OAuth browser flow.
- **`Sheet header mismatch`** — the destination tab has a non-empty row 1 that doesn't match `reference/columns.md`. Either fix the sheet's header row to match, or update the schema file.
- **0 rows even though Tally has journals** — every journal in the range posts only against non-debtor ledgers (expense reclassifications, depreciation, vendor adjustments). Confirm by sampling a journal in Tally — its Dr/Cr legs should include at least one Sundry Debtor for the row to appear here.
- **Duplicate rows after a run** — confirm the dedupe key is still `(Company, Location, Voucher No., Particulars, Reference Invoice Number)` in `push_journals_to_sheet.py`. If `reference/columns.md` keys were renamed, the script needs the new key names.
- **Multiple rows for the same voucher** — expected. A journal that settles six original invoices in a single ₹10L credit produces six rows (one per `BILLALLOCATIONS.LIST` entry). Multiple debtor legs in one journal also produce multiple rows. The composite dedupe key keeps re-runs safe.
- **`Reference Invoice Number` is blank** — the debtor leg has no bill allocations (an `On Account` posting). The row carries the full leg amount in this case; this is a valid journal that simply doesn't pin to a specific original invoice.
