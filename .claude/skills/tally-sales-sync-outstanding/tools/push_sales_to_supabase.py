"""Push fetched sales rows to **Supabase** (parallel destination to the Google
Sheets pusher; both can run for the same input — they don't interfere).

Two tables are written, mirroring the two sheets:

1. ``sales_vouchers`` — per-voucher summary; schema from ``reference/columns.md``,
   dedupe ``(company, location, voucher_no, date)``.
2. ``sales_outstanding_register`` — bill-wise detail; schema from
   ``reference/columns_details.md``, dedupe
   ``(company, location, voucher_no, bill_ref_name, date)``.

Idempotent: existing rows in the date range are queried first, the input is
partitioned into "new" vs "skipped", and only new rows are inserted. The
unique constraints in Postgres are the safety net — a race or a bug here
will surface as a constraint violation, not a duplicate row.

Reads the same JSON shape as ``push_sales_to_sheet.py`` (new dict form
``{"vouchers": [...], "details": [...]}`` or legacy flat list).

Required env vars (loaded from project .env):
    SUPABASE_URL           e.g. https://<ref>.supabase.co (no /rest/v1 suffix)
    SUPABASE_SERVICE_KEY   service_role / secret key (bypasses RLS)

Usage:
    python push_sales_to_supabase.py --input .tmp/sales_<ts>.json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from dotenv import load_dotenv
from supabase import create_client, Client

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _schema import Column, load_columns, load_details_columns  # noqa: E402


VOUCHERS_TABLE = "sales_vouchers"
DETAILS_TABLE = "sales_outstanding_register"

DEDUPE_KEYS_VOUCHERS = ("company", "location", "voucher_no", "date")
DEDUPE_KEYS_DETAILS = ("company", "location", "voucher_no", "bill_ref_name", "date")

# Per-table column-type hints. Anything not listed is treated as text.
NUMERIC_KEYS = {"quantity", "rate", "value", "gross_total", "bill_amount"}
DATE_KEYS = {"date", "due_date"}

# Supabase select() default page size; we paginate to handle large date ranges.
PAGE_SIZE = 1000


def _to_iso_date(s: str) -> str | None:
    """``DD/MM/YYYY`` → ``YYYY-MM-DD``. Empty / unparseable → ``None``."""
    s = (s or "").strip()
    if not s:
        return None
    try:
        return datetime.strptime(s, "%d/%m/%Y").strftime("%Y-%m-%d")
    except ValueError:
        # Fetch script occasionally falls through to raw YYYYMMDD on parse error.
        try:
            return datetime.strptime(s, "%Y%m%d").strftime("%Y-%m-%d")
        except ValueError:
            return None


def _to_numeric(s: Any) -> float | None:
    """Empty / unparseable → ``None``. Otherwise ``float``."""
    if s is None:
        return None
    s = str(s).strip()
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _coerce_row(row: dict[str, Any], columns: list[Column]) -> dict[str, Any]:
    """Coerce a fetch-script row into Postgres-friendly types.

    Strings stay strings (empty stays empty — empty strings participate in
    unique-constraint dedupe, NULLs don't). Numerics and dates are converted
    or set to ``None``.
    """
    out: dict[str, Any] = {}
    for col in columns:
        v = row.get(col.key, "")
        if col.key in DATE_KEYS:
            out[col.key] = _to_iso_date(v if isinstance(v, str) else str(v))
        elif col.key in NUMERIC_KEYS:
            out[col.key] = _to_numeric(v)
        else:
            out[col.key] = (str(v) if v is not None else "").strip()
    return out


def _row_dedupe_key(row: dict[str, Any], keys: tuple[str, ...]) -> tuple[str, ...]:
    """Build the dedupe-key tuple from a coerced row.

    Dates are already ISO ``YYYY-MM-DD`` strings or ``None`` after coercion;
    we stringify ``None`` to ``""`` so the tuple compares cleanly against
    keys built from existing-row queries.
    """
    parts: list[str] = []
    for k in keys:
        v = row.get(k)
        parts.append("" if v is None else str(v).strip())
    return tuple(parts)


def _fetch_existing_keys(
    client: Client,
    table: str,
    dedupe_keys: tuple[str, ...],
    rows: list[dict[str, Any]],
) -> set[tuple[str, ...]]:
    """Return the set of dedupe-key tuples already present in ``table`` for
    the date range and companies covered by ``rows``.

    Scoping the query to the input's date+company range keeps the query
    small even when the table holds years of history.
    """
    if not rows:
        return set()

    dates = sorted({r["date"] for r in rows if r.get("date")})
    companies = sorted({r["company"] for r in rows if r.get("company")})
    if not dates or not companies:
        return set()

    date_min, date_max = dates[0], dates[-1]
    select_cols = ",".join(dedupe_keys)

    seen: set[tuple[str, ...]] = set()
    offset = 0
    while True:
        resp = (
            client.table(table)
            .select(select_cols)
            .in_("company", companies)
            .gte("date", date_min)
            .lte("date", date_max)
            .range(offset, offset + PAGE_SIZE - 1)
            .execute()
        )
        batch = resp.data or []
        for r in batch:
            seen.add(_row_dedupe_key(r, dedupe_keys))
        if len(batch) < PAGE_SIZE:
            break
        offset += PAGE_SIZE
    return seen


def _chunked(seq: list[Any], n: int) -> Iterable[list[Any]]:
    for i in range(0, len(seq), n):
        yield seq[i : i + n]


def push_table(
    client: Client,
    table: str,
    columns: list[Column],
    rows_in: list[dict[str, Any]],
    dedupe_keys: tuple[str, ...],
) -> dict[str, Any]:
    coerced = [_coerce_row(r, columns) for r in rows_in]
    existing = _fetch_existing_keys(client, table, dedupe_keys, coerced)

    new_rows: list[dict[str, Any]] = []
    skipped = 0
    seen_within_batch: set[tuple[str, ...]] = set()
    for r in coerced:
        key = _row_dedupe_key(r, dedupe_keys)
        if key in existing or key in seen_within_batch:
            skipped += 1
            continue
        seen_within_batch.add(key)
        new_rows.append(r)

    # Insert in chunks; Supabase's PostgREST handles batches comfortably up
    # to ~1000 rows per request.
    for chunk in _chunked(new_rows, 500):
        client.table(table).insert(chunk).execute()

    return {
        "fetched": len(rows_in),
        "appended": len(new_rows),
        "skipped": skipped,
        "table": table,
    }


def _split_input(raw: Any) -> tuple[list[dict[str, Any]], list[dict[str, Any]] | None]:
    if isinstance(raw, list):
        return raw, None
    if isinstance(raw, dict):
        return raw.get("vouchers", []), raw.get("details", [])
    raise SystemExit(f"ERROR: unexpected input JSON shape: {type(raw).__name__}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Push fetched sales JSON to Supabase (vouchers + bill-wise details).")
    parser.add_argument("--input", required=True, help="Path to JSON file produced by fetch_tally_sales.py")
    args = parser.parse_args()

    load_dotenv()
    url = os.environ["SUPABASE_URL"].strip().rstrip("/")
    key = os.environ["SUPABASE_SERVICE_KEY"].strip()
    if url.endswith("/rest/v1"):
        url = url[: -len("/rest/v1")]

    client = create_client(url, key)

    voucher_columns = load_columns()
    detail_columns = load_details_columns()
    raw = json.loads(Path(args.input).read_text(encoding="utf-8"))
    voucher_rows, detail_rows = _split_input(raw)

    voucher_summary = push_table(
        client, VOUCHERS_TABLE, voucher_columns, voucher_rows, DEDUPE_KEYS_VOUCHERS
    )

    if detail_rows is None:
        print(
            "WARNING: input JSON is in the legacy flat-list shape — skipping "
            "the bill-wise detail push. Re-fetch with the current "
            "fetch_tally_sales.py to populate sales_outstanding_register.",
            file=sys.stderr,
        )
        details_summary = {"fetched": 0, "appended": 0, "skipped": 0, "skipped_legacy": True}
    else:
        details_summary = push_table(
            client, DETAILS_TABLE, detail_columns, detail_rows, DEDUPE_KEYS_DETAILS
        )

    summary = {
        "fetched": voucher_summary["fetched"],
        "appended": voucher_summary["appended"],
        "skipped": voucher_summary["skipped"],
        "table": voucher_summary["table"],
        "details": details_summary,
    }
    print(json.dumps(summary))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
