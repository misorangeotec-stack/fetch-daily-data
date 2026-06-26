"""Upsert fetched Balance Sheet rows into the Balance Sheet Google Sheet.

For each fetched row, the upsert key is the composite ``(Company, Location,
group_id)``. If a sheet row already has that key, the script **updates the
schema-managed columns in place** (preserving any extra columns the user has
added to the right). If the key is new, the row is **appended** at the bottom.
A re-run is a latest-snapshot refresh: balances and ``as_of_date`` update,
the row is not duplicated.

Timestamp handling:
  * ``created_at`` and ``updated_at`` are populated by this push tool, not by
    the fetch tool (the fetch JSON leaves them blank).
  * On insert, both timestamps are set to the current run's UTC ISO 8601.
  * On update, the existing ``created_at`` is preserved; ``updated_at`` is
    bumped only when the row's other schema columns actually change.
  * The "did the row change?" comparison **excludes** the timestamp columns
    so that re-running on unchanged data reports `unchanged`, not `updated`.

Required env vars (loaded from project .env):
    GOOGLE_CREDENTIALS_FILE      path to OAuth client secrets JSON
    GOOGLE_TOKEN_FILE            path to cached OAuth token JSON
    BALANCE_SHEET_SHEET_URL      full URL of the destination Google Sheet
    BALANCE_SHEET_SHEET_TAB      tab/sheet name within the spreadsheet

Usage:
    python push_balance_sheet_to_sheet.py --input .tmp/balance_sheet_<ts>.json
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _schema import Column, load_columns  # noqa: E402


SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
SHEET_ID_RE = re.compile(r"/spreadsheets/d/([a-zA-Z0-9-_]+)")
UPSERT_KEYS = ("company", "location", "group_id")  # composite key for upsert

SHEET_URL_ENV = "BALANCE_SHEET_SHEET_URL"
SHEET_TAB_ENV = "BALANCE_SHEET_SHEET_TAB"

# These columns are managed by the push tool (not Tally-sourced), so they
# must be excluded from the "did the row change?" comparison — otherwise
# every push would mark every row as updated.
TIMESTAMP_KEYS = ("created_at", "updated_at")


def now_iso() -> str:
    """Single timestamp for this run (UTC, ISO 8601, second precision)."""
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def extract_sheet_id(url: str) -> str:
    m = SHEET_ID_RE.search(url)
    if not m:
        raise SystemExit(f"ERROR: could not extract sheet ID from {SHEET_URL_ENV}='{url}'")
    return m.group(1)


def authorize() -> Any:
    creds_path = os.environ["GOOGLE_CREDENTIALS_FILE"]
    token_path = os.environ["GOOGLE_TOKEN_FILE"]
    creds: Credentials | None = None
    if Path(token_path).exists():
        creds = Credentials.from_authorized_user_file(token_path, SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(creds_path, SCOPES)
            creds = flow.run_local_server(port=0)
        Path(token_path).write_text(creds.to_json(), encoding="utf-8")
    return build("sheets", "v4", credentials=creds, cache_discovery=False)


def get_existing_rows(svc: Any, sheet_id: str, tab: str) -> list[list[str]]:
    resp = svc.spreadsheets().values().get(
        spreadsheetId=sheet_id,
        range=f"'{tab}'",
        majorDimension="ROWS",
    ).execute()
    return resp.get("values", [])


def ensure_header(svc: Any, sheet_id: str, tab: str, columns: list[Column], existing: list[list[str]]) -> None:
    expected = [c.header for c in columns]
    if not existing:
        _write_header(svc, sheet_id, tab, expected)
        return
    actual = [c.strip() for c in existing[0]]
    actual_trimmed = actual[: len(expected)]
    if all(c == "" for c in actual):
        _write_header(svc, sheet_id, tab, expected)
        return
    if actual_trimmed != expected:
        raise SystemExit(
            "ERROR: sheet header row does not match reference/columns.md.\n"
            f"  Expected: {expected}\n"
            f"  Found:    {actual}\n"
            "Either fix the sheet's row 1 to match, or update reference/columns.md."
        )


def _write_header(svc: Any, sheet_id: str, tab: str, headers: list[str]) -> None:
    svc.spreadsheets().values().update(
        spreadsheetId=sheet_id,
        range=f"'{tab}'!A1",
        valueInputOption="RAW",
        body={"values": [headers]},
    ).execute()


def _header_for(columns: list[Column], key: str) -> str:
    for c in columns:
        if c.key == key:
            return c.header
    raise ValueError(f"Schema is missing required key '{key}'")


def existing_key_to_row(existing: list[list[str]], columns: list[Column]) -> dict[tuple[str, ...], int]:
    """Return a map from upsert-key tuple to 1-based sheet row number."""
    if len(existing) <= 1:
        return {}
    headers = [c.strip() for c in existing[0]]
    try:
        col_indices = [headers.index(_header_for(columns, k)) for k in UPSERT_KEYS]
    except ValueError as exc:
        raise SystemExit(f"ERROR: upsert column missing from sheet headers: {exc}")

    out: dict[tuple[str, ...], int] = {}
    for offset, row in enumerate(existing[1:], start=2):
        key = tuple(row[i].strip() if i < len(row) else "" for i in col_indices)
        if any(key):
            out[key] = offset
    return out


def _normalize(v: Any) -> str:
    """Normalize a cell value for equality comparison."""
    s = "" if v is None else str(v).strip()
    if s == "":
        return ""
    try:
        f = float(s.replace(",", ""))
        return f"{f:.6f}".rstrip("0").rstrip(".")
    except ValueError:
        return s


def _col_letter(idx_zero_based: int) -> str:
    """0 → A, 25 → Z, 26 → AA …"""
    n = idx_zero_based
    letters = ""
    while True:
        n, r = divmod(n, 26)
        letters = chr(ord("A") + r) + letters
        if n == 0:
            break
        n -= 1
    return letters


def _key_index_in_columns(columns: list[Column], key: str) -> int:
    for i, c in enumerate(columns):
        if c.key == key:
            return i
    raise ValueError(f"key '{key}' not in columns")


def main() -> int:
    parser = argparse.ArgumentParser(description="Upsert Tally Balance Sheet JSON into Google Sheets.")
    parser.add_argument("--input", required=True, help="Path to JSON file produced by fetch_tally_balance_sheet.py")
    args = parser.parse_args()

    load_dotenv()
    sheet_url = os.environ[SHEET_URL_ENV]
    tab = os.environ.get(SHEET_TAB_ENV, "Sheet1")
    sheet_id = extract_sheet_id(sheet_url)

    columns = load_columns()
    rows_in: list[dict[str, Any]] = json.loads(Path(args.input).read_text(encoding="utf-8"))

    svc = authorize()
    existing = get_existing_rows(svc, sheet_id, tab)
    ensure_header(svc, sheet_id, tab, columns, existing)
    if not existing or all(c == "" for c in (existing[0] if existing else [])):
        existing = get_existing_rows(svc, sheet_id, tab)

    key_to_sheet_row = existing_key_to_row(existing, columns)

    last_col_letter = _col_letter(len(columns) - 1)

    try:
        created_at_idx = _key_index_in_columns(columns, "created_at")
        updated_at_idx = _key_index_in_columns(columns, "updated_at")
    except ValueError:
        created_at_idx = -1
        updated_at_idx = -1
    excluded_idxs = {i for i in (created_at_idx, updated_at_idx) if i >= 0}

    run_ts = now_iso()

    update_data: list[dict[str, Any]] = []
    new_rows: list[list[Any]] = []
    appended = 0
    updated = 0
    unchanged = 0

    for r in rows_in:
        key = tuple(str(r.get(k, "")).strip() for k in UPSERT_KEYS)
        if not all(key):
            continue

        new_values: list[Any] = [r.get(c.key, "") for c in columns]

        if key in key_to_sheet_row:
            sheet_row = key_to_sheet_row[key]
            existing_values = existing[sheet_row - 1] if sheet_row - 1 < len(existing) else []
            existing_trimmed = [
                (existing_values[i] if i < len(existing_values) else "")
                for i in range(len(columns))
            ]

            existing_compare = [
                _normalize(v) for i, v in enumerate(existing_trimmed) if i not in excluded_idxs
            ]
            new_compare = [
                _normalize(v) for i, v in enumerate(new_values) if i not in excluded_idxs
            ]
            if existing_compare == new_compare:
                unchanged += 1
                continue

            if created_at_idx >= 0:
                new_values[created_at_idx] = (
                    existing_trimmed[created_at_idx] or run_ts
                )
            if updated_at_idx >= 0:
                new_values[updated_at_idx] = run_ts

            update_data.append({
                "range": f"'{tab}'!A{sheet_row}:{last_col_letter}{sheet_row}",
                "values": [new_values],
            })
            updated += 1
        else:
            if created_at_idx >= 0:
                new_values[created_at_idx] = run_ts
            if updated_at_idx >= 0:
                new_values[updated_at_idx] = run_ts
            new_rows.append(new_values)
            appended += 1

    if update_data:
        svc.spreadsheets().values().batchUpdate(
            spreadsheetId=sheet_id,
            body={"valueInputOption": "RAW", "data": update_data},
        ).execute()

    if new_rows:
        svc.spreadsheets().values().append(
            spreadsheetId=sheet_id,
            range=f"'{tab}'!A1",
            valueInputOption="RAW",
            insertDataOption="INSERT_ROWS",
            body={"values": new_rows},
        ).execute()

    summary = {
        "fetched": len(rows_in),
        "appended": appended,
        "updated": updated,
        "unchanged": unchanged,
        "sheet_url": sheet_url,
    }
    print(json.dumps(summary))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
