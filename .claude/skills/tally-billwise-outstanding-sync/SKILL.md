---
name: tally-billwise-outstanding-sync
description: Sync each Sundry-Debtor ledger's CURRENT bill-wise outstanding (open bills with due dates, Tally's BILLCL as-on the snapshot date) from local Tally Prime into the Tally Bill-wise Outstanding Google Sheet. Use whenever the user asks to fetch/pull/sync/export the Tally bill-wise outstanding, refresh the bill-wise sheet, or run the daily/periodic bill-wise sync that the dashboard uses to source Tally-true outstanding/overdue/aging. Fetches per-ledger (the only stable route — whole-company bill dumps hang/crash Tally), one company at a time, paced; emits one row per open bill (Company, Location, ledger_id, $Name, Bill Ref Name, Bill Date, Due Date, Closing Balance, Dr/Cr); snapshot-replaces by (Company, Location) so paid bills drop out.
---

# Tally Bill-wise Outstanding Sync

Syncs every Sundry-Debtor ledger's **current open-bill set** (with due dates)
from local Tally Prime into a Google Sheet. The Orange Receivables Hub's
`process_data.py` reads this sheet (`TALLY_BILLWISE_SHEET_URL`/`_TAB`) and, for
any party whose calculated outstanding disagrees with Tally, sources that
party's **outstanding + overdue + aging** straight from these bills — so the
dashboard mirrors Tally's actual receivable. See `OVERDUE_RECONCILE_PLAN.md`
§2C / `RESYNC_28_PLAN.md` in the Hub.

## Why per-ledger

Whole-company bill / outstanding collections HANG (~180s) and can crash the
Tally gateway on this build. The per-ledger **Ledger Outstandings** report
returns in ~0.2s with each bill's date, ref, balance and DUE DATE (incl.
future-dated installments). So the fetch lists Sundry-Debtor ledgers, then
pulls bills one ledger at a time, sequentially, with a small delay
(`--delay`, default 0.2s). One company at a time — never parallel.

## Tools

- `tools/fetch_tally_billwise.py` — list debtors (Group+Ledger collections,
  parent-chain walk) → per-ledger BILLCL fetch → JSON rows.
  `--companies "<raw Tally co>" --output <json> [--as-of YYYY-MM-DD] [--delay 0.2]`
- `tools/push_billwise_to_sheet.py` — **snapshot replace** by (Company, Location):
  drops existing rows for the synced scope, writes the fresh snapshot, preserves
  `created_at` per bill. `--input <json>`
- `tools/list_tally_companies.py`, `tools/_schema.py` — shared helpers.
- `reference/columns.md` — sheet schema (source of truth). `reference/companies.md`
  — raw Tally name → (Company, Location).

## Env (project .env)

- `TALLY_HOST` — e.g. `http://localhost:9000`
- `TALLY_BILLWISE_SHEET_URL` / `TALLY_BILLWISE_SHEET_TAB` — destination sheet.

## Daily workflow

Registered as the **"Bill-wise Outstanding"** master in the Streamlit sync
dashboard (`tools/sync_dashboard`), so the daily refresh fetches + pushes it
alongside Sales / Bank Receipt / etc., then `process_data → Supabase` consumes
it automatically. It is the slowest master (per-ledger, ~700 ledgers) — being a
snapshot, it can run daily or less often; the dashboard uses the last snapshot
until refreshed.

## Guardrails

- localhost:9000 only; per-ledger, sequential, paced; one company at a time.
- Never whole-company bill/outstanding dumps.
- Snapshot replace is scoped to the companies in the input — other scopes are
  preserved.
- **Prior-year (inactive) books are skipped for bill-wise.** Companies whose
  number is in `TALLY_INACTIVE_COMPANIES` (.env) are auto-skipped by both the
  fetcher (`fetch_tally_billwise.py`) and the dashboard (`build_steps`), because a
  frozen prior-FY book still lists open bills the LIVE book has since carried
  forward (double-count) or seen paid (phantom) — see the FY-split note in
  `reference/companies.md`. Flows/masters still sync from those books (history);
  only bill-wise is blocked. Override on the fetcher with `--allow-inactive`.
