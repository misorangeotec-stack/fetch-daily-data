# tools/

Deterministic Python scripts that do the actual work — API calls, data transforms, file I/O, DB queries.

## Conventions

- One script per task. Keep them small and testable.
- Read secrets from `.env` via `python-dotenv` — never hardcode credentials.
- Accept inputs via CLI args or a clearly-typed `main(...)` function.
- Print structured progress to stdout; write outputs to `.tmp/` or directly to the cloud destination defined by the workflow.
- Exit non-zero on failure with a clear error message.

## Adding a new tool

1. Check whether an existing tool already does the job.
2. If not, name the script after the action it performs (e.g. `fetch_sales_report.py`, `push_to_sheets.py`).
3. Document required env vars and inputs at the top of the file.
4. Reference the tool from the workflow that uses it.
