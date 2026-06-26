# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this project does

Daily-data sync layer for Orange O Tec: pulls vouchers and master data from **local Tally Prime** (XML over HTTP, default `http://localhost:9000`) and pushes them into per-master **Google Sheets** that feed the outstanding (receivables) report. Each data domain (Sales, Sales Credit Note, Bank Receipt, Cheque Return, Credit Limit & Opening, Ledger Master) is implemented as its own self-contained skill.

## WAT architecture (Workflows / Agents / Tools)

Three layers, deliberately separated so deterministic Python handles execution and the agent only orchestrates:

- **Workflows** — markdown SOPs (per-skill `workflows/*.md` and per-skill `SKILL.md`). Define objective, inputs, tool order, outputs, edge cases.
- **Agent (you)** — read the workflow, call tools in order, handle failures, ask clarifying questions. Don't try to do the work directly.
- **Tools** — Python scripts that do API calls, transforms, sheet I/O. Live inside each skill's `tools/` folder, not at project root.

Why: chained 90%-accurate AI steps collapse fast (5 steps → 59%). Push execution into scripts; keep reasoning in the agent.

When something fails: read the trace, fix the script, retest, then update the workflow with what you learned. Don't overwrite or rewrite workflows without asking — they are durable instructions.

## Project layout

```
.claude/skills/<skill-name>/   # Self-contained skill (SKILL.md + tools/ + workflows/ + reference/)
tools/sync_dashboard/          # Streamlit UI that orchestrates all 5 outstanding-report skills
workflows/                     # (top-level, mostly empty — workflows now live inside each skill)
references/                    # Sample .xlsx files showing target sheet schemas
.tmp/                          # Disposable intermediate JSON between fetch and push
.env                           # Tally host, company creds, per-skill SHEET_URL/SHEET_TAB
credentials.json, token.json   # Google OAuth (gitignored)
```

Skill folders follow a consistent shape: `SKILL.md` (entry point), `tools/fetch_*.py` + `tools/push_*.py` + shared `_schema.py` + `list_tally_companies.py`, `reference/columns.md` (mutable column schema — edit this, not the .py), `workflows/*.md` (SOP).

## Common commands

```bash
# One-time setup
pip install -r requirements.txt
pip install -r tools/sync_dashboard/requirements.txt

# Launch the orchestrator dashboard (preferred — non-Claude users can run it)
streamlit run tools/sync_dashboard/app.py    # opens http://localhost:8501

# Run a skill manually (example: sales)
python .claude/skills/tally-sales-sync-outstanding/tools/list_tally_companies.py
python .claude/skills/tally-sales-sync-outstanding/tools/fetch_tally_sales.py \
    --from 01-04-2026 --to 30-04-2026 --companies "Co Name" --output .tmp/sales.json
python .claude/skills/tally-sales-sync-outstanding/tools/push_sales_to_sheet.py --input .tmp/sales.json
```

Date format throughout the codebase is `DD-MM-YYYY`. The Indian financial year starts **1 April** (`fy_start_for()` in [orchestrator.py](tools/sync_dashboard/orchestrator.py)).

## Cross-cutting conventions (load-bearing)

- **Tally fetches must be chunked per calendar month.** A single Tally HTTP read times out beyond ~1 month. The dashboard does this automatically via `month_chunks()`; manual invocations of `fetch_*.py` for ranges >1 month must loop month-by-month.
- **Push scripts dedupe against the destination sheet.** Re-running with overlapping ranges is safe. Dedupe keys vary per skill (e.g. sales = `(Company, Voucher No.)`, credit-note = `(Company, Location, Voucher No.)`); each push script's source-of-truth is its own header.
- **All push scripts default to a content-aware voucher UPSERT, not append** (`_upsert_by_voucher`; for sales it lives in `push_to_sheet`). Why: a voucher can CHANGE in Tally after its first sync — the classic case is a receipt booked **"On Account"** (blank ref) later **allocated "Agst Ref"** against an invoice; also amount corrections, changed references, edited bill splits. The old append-by-composite-key treated the re-allocated row as new → **duplicate** (the stale On-Account row lingered). The upsert groups rows by the voucher identity `UPSERT_KEYS` = `(company, location, voucher_no[, customer_name/particulars])` (sales groups by `dedupe_keys[:3]`, covering both the per-voucher sheet and the bill-wise Register), and for each voucher in the batch **compares its full row-set to the sheet's**: identical → skip (no rewrite); changed (ANY column) → `deleteDimension` the sheet's rows for that voucher + re-append; new → plain append. So **any Tally edit is reflected on the next covering sync, never duplicated**, it's idempotent, and a forward sync of all-new vouchers is a plain append (no deletes → same speed). Unit + live tested (`.tmp/test_upsert*.py`).
- **"From last pending" re-fetches a rolling `SYNC_OVERLAP_DAYS` (default 30) buffer before the watermark** ([orchestrator.py](tools/sync_dashboard/orchestrator.py) `OVERLAP_DAYS`). A re-allocation happens to a *past-dated* voucher, so a pure forward sync wouldn't re-pull it; the buffer re-covers recent days and the upsert fixes it automatically — **no "Full reconcile" needed** for the on-account→invoice case. Set `SYNC_OVERLAP_DAYS=0` for the old pure-forward behaviour; widen it to catch older re-allocations.
- **Column schemas live in `reference/columns.md`** inside each skill, parsed by the shared `_schema.py`. To add/rename/reorder a column, edit the markdown table — do not hardcode column lists in Python.
- **Sheets writes use `RAW`, never `USER_ENTERED`.** `USER_ENTERED` corrupts phone-number strings and rounds large numbers on round-trip.
- **Last-line JSON contract.** Every push script's final stdout line is a JSON object (`{"fetched": N, "appended": N, "skipped": N, ...}`). The dashboard parses this; preserve the contract when editing scripts.
- **Multi-company.** Tally can have several companies loaded simultaneously. Tools accept `--companies "A,B,C"`; skills iterate and tag each row with its source company.
- **Credentials never leave `.env`.** Don't hardcode tokens, sheet URLs, or Tally creds in Python or commit them. `.env`, `credentials.json`, `token.json` are gitignored.

## The sync dashboard

[tools/sync_dashboard/app.py](tools/sync_dashboard/app.py) is a Streamlit UI that lets the team run all 5 outstanding-report syncs without invoking Claude. Architecture:

- [app.py](tools/sync_dashboard/app.py) — UI (setup view + running view).
- [orchestrator.py](tools/sync_dashboard/orchestrator.py) — `MASTERS` registry, step builder (per-month chunks per company), `Runner` (sequential subprocess driver, captures last JSON line per step).
- [state.py](tools/sync_dashboard/state.py) — persists `sync_state.json` (last-sync watermark per master×company + duration history for ETA). "From last pending" mode resumes from `last_sync.to_date + 1 day`.
- [tally_companies.py](tools/sync_dashboard/tally_companies.py) — dynamically loads `list_companies()` from one of the skills rather than duplicating the XML.

When adding a new outstanding-report skill, register it in `MASTERS` and `UI_ORDER` in [orchestrator.py](tools/sync_dashboard/orchestrator.py:41) and add a default duration in [state.py](tools/sync_dashboard/state.py:18).

## When you make changes

- Run only one dashboard instance at a time (`sync_state.json` has no lock).
- After editing a fetch/push script, smoke-test with a 1-day range against one company before recommending a full re-run.
- Delete `token.json` to force OAuth re-auth if Google API calls start 401-ing.
- If Tally returns `Connection refused`, Tally Prime isn't running or HTTP/XML isn't enabled (`F1 → Settings → Connectivity → TallyPrime acting as: Both`).
