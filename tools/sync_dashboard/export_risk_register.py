"""Export the Customer Risk Register to an Excel workbook.

Read-only: pulls the receivables ``customers`` table from Supabase (the same
table the live dashboard's Risk Register reads) and writes one **.xlsx** row per
``(customer, company, location)`` — Company and Location as their own columns,
and the full aging breakdown (0-30 … 180+) as separate columns alongside the
total Overdue. No Supabase writes; nothing in Tally is touched.

The ``customers`` table already stores a pre-computed ``aging_buckets`` JSON per
row (the exact numbers the dashboard's Aging filter shows), so this script only
reads, flattens, and formats — it does not recompute aging.

Required env vars (loaded from project .env):
    SUPABASE_URL           e.g. https://<ref>.supabase.co (no /rest/v1 suffix)
    SUPABASE_SERVICE_KEY   service_role / secret key (bypasses RLS)

Usage:
    python export_risk_register.py
    python export_risk_register.py --fiscal-year default --out-dir ../../MISC
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from supabase import create_client, Client

from openpyxl import Workbook
from openpyxl.styles import Font, Alignment
from openpyxl.utils import get_column_letter

# tools/sync_dashboard/ -> project root is two levels up.
PROJECT_ROOT = Path(__file__).resolve().parents[2]

# PostgREST caps a single response at 1000 rows; page through with .range().
PAGE_SIZE = 1000

# Internal aging-bucket keys (in aging_buckets JSON) -> human column header.
AGING_BUCKETS: list[tuple[str, str]] = [
    ("0_30", "0-30"),
    ("31_60", "31-60"),
    ("61_90", "61-90"),
    ("91_120", "91-120"),
    ("121_180", "121-180"),
    ("180_plus", "180+"),
]

# (header, source-row key, is_amount) — column order matches the approved plan.
# Aging buckets + "Aging Total" are injected between "Overdue" and "Max OD Days".
BASE_COLUMNS: list[tuple[str, str, bool]] = [
    ("Customer", "name", False),
    ("Company", "company", False),
    ("Location", "location", False),
    ("Sales Person", "sales_person", False),
    ("Opening", "opening_balance", True),
    ("Sales", "sales", True),
    ("Receipts", "receipts", True),
    ("Credit Notes", "credit_notes", True),
    ("Debit Notes", "debit_notes", True),
    ("Journal (Net)", "journal_adjustments", True),
    ("Outstanding", "outstanding", True),
    ("Overdue", "overdue", True),
]

TAIL_COLUMNS: list[tuple[str, str, bool]] = [
    ("Max OD Days", "max_overdue_days", False),
    ("Credit Period", "credit_period", False),
    ("Credit Limit", "credit_limit", True),
    ("Util %", "utilization", False),
    ("Risk", "risk", False),
]

# Excel number format for rupee amounts: thousands separators, no decimals.
AMOUNT_FMT = "#,##0"


def _to_number(v: Any) -> float | int:
    """Coerce a Supabase numeric/string to a number; blanks/garbage -> 0."""
    if v is None or v == "":
        return 0
    try:
        f = float(v)
    except (TypeError, ValueError):
        return 0
    return int(f) if f == int(f) else f


def _aging(row: dict[str, Any]) -> dict[str, float | int]:
    """Return the six bucket values for a row, defaulting missing keys to 0.

    aging_buckets may arrive as a dict (jsonb) or a JSON string depending on the
    client/driver — normalise both.
    """
    raw = row.get("aging_buckets")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            raw = {}
    if not isinstance(raw, dict):
        raw = {}
    return {key: _to_number(raw.get(key)) for key, _ in AGING_BUCKETS}


def fetch_customers(client: Client, fiscal_year: str) -> list[dict[str, Any]]:
    """Page through the customers table for one fiscal-year snapshot."""
    rows: list[dict[str, Any]] = []
    offset = 0
    while True:
        resp = (
            client.table("customers")
            .select("*")
            .eq("fiscal_year", fiscal_year)
            .range(offset, offset + PAGE_SIZE - 1)
            .execute()
        )
        batch = resp.data or []
        rows.extend(batch)
        if len(batch) < PAGE_SIZE:
            break
        offset += PAGE_SIZE
    return rows


def build_workbook(rows: list[dict[str, Any]]) -> Workbook:
    wb = Workbook()
    ws = wb.active
    ws.title = "Risk Register"

    headers = (
        [h for h, _, _ in BASE_COLUMNS]
        + [label for _, label in AGING_BUCKETS]
        + ["Aging Total"]
        + [h for h, _, _ in TAIL_COLUMNS]
    )
    # is_amount flag per column, aligned with `headers`.
    amount_flags = (
        [amt for _, _, amt in BASE_COLUMNS]
        + [True] * len(AGING_BUCKETS)
        + [True]                      # Aging Total
        + [amt for _, _, amt in TAIL_COLUMNS]
    )

    ws.append(headers)
    header_font = Font(bold=True)
    for cell in ws[1]:
        cell.font = header_font
        cell.alignment = Alignment(vertical="center")

    for row in rows:
        buckets = _aging(row)
        aging_total = sum(buckets.values())
        values: list[Any] = []
        for _, key, is_amount in BASE_COLUMNS:
            values.append(_to_number(row.get(key)) if is_amount else (row.get(key) or ""))
        values.extend(buckets[key] for key, _ in AGING_BUCKETS)
        values.append(aging_total)
        for _, key, is_amount in TAIL_COLUMNS:
            values.append(_to_number(row.get(key)) if is_amount else (row.get(key) or ""))
        ws.append(values)

    # Apply amount number-format and compute auto widths.
    widths = [len(h) for h in headers]
    for r in range(2, ws.max_row + 1):
        for c in range(1, len(headers) + 1):
            cell = ws.cell(row=r, column=c)
            if amount_flags[c - 1] and isinstance(cell.value, (int, float)):
                cell.number_format = AMOUNT_FMT
            text = f"{cell.value:,.0f}" if amount_flags[c - 1] and isinstance(cell.value, (int, float)) else str(cell.value or "")
            if len(text) > widths[c - 1]:
                widths[c - 1] = len(text)

    for c, w in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(c)].width = min(max(w + 2, 8), 48)

    ws.freeze_panes = "A2"
    return wb


def main() -> int:
    parser = argparse.ArgumentParser(description="Export the Customer Risk Register to Excel.")
    parser.add_argument("--fiscal-year", default="default",
                        help="customers.fiscal_year snapshot to export (default / fy2526 / fy2627)")
    parser.add_argument("--out-dir", default=str(PROJECT_ROOT / "MISC"),
                        help="Directory to write the .xlsx into (default: <project>/MISC)")
    args = parser.parse_args()

    load_dotenv()
    url = os.environ["SUPABASE_URL"].strip().rstrip("/")
    key = os.environ["SUPABASE_SERVICE_KEY"].strip()
    if url.endswith("/rest/v1"):
        url = url[: -len("/rest/v1")]

    client = create_client(url, key)
    rows = fetch_customers(client, args.fiscal_year)

    wb = build_workbook(rows)

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%d-%m-%Y")
    out_path = out_dir / f"risk-register-{stamp}.xlsx"
    wb.save(out_path)

    print(json.dumps({
        "rows": len(rows),
        "file": str(out_path),
        "fiscal_year": args.fiscal_year,
    }))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
