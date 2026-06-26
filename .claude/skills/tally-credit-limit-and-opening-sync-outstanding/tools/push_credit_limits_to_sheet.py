"""Upsert fetched Sundry Debtor master rows into the credit-limit Google Sheet.

For each fetched row, the upsert key is the composite ``(Company, Location,
$Name)``. If a sheet row already has that key, the script **updates the
schema-managed columns in place** (preserving any extra columns the user has
added to the right). If the key is new, the row is **appended** at the
bottom.

Required env vars (loaded from project .env):
    GOOGLE_CREDENTIALS_FILE      path to OAuth client secrets JSON
    GOOGLE_TOKEN_FILE            path to cached OAuth token JSON
    CREDIT_LIMIT_SHEET_URL       full URL of the destination Google Sheet
    CREDIT_LIMIT_SHEET_TAB       tab/sheet name within the spreadsheet

Usage:
    python push_credit_limits_to_sheet.py --input .tmp/credit_limits_<ts>.json
"""

from __future__ import annotations

import argparse
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
# Primary upsert key = the Tally GUID (`ledger_id`), which is STABLE across
# ledger renames. Keying on the name triplet instead would append a brand-new
# row whenever a ledger's name changed by even a dot/space — that produced 65+
# duplicate rows historically. UPSERT_KEYS is the NAME-based fallback used only
# for the rare row that has no ledger_id (legacy/manual entries).
ID_KEY = "ledger_id"
UPSERT_KEYS = ("company", "location", "name")  # name-based fallback key


def _upsert_key(get: "Any") -> tuple[str, ...]:
    """Derive a row's upsert key from a field getter ``get(column_key) -> str``.

    Prefers the stable Tally GUID; falls back to the (company, location, name)
    triplet only when ``ledger_id`` is blank. The leading tag keeps the two
    key spaces from ever colliding.
    """
    lid = str(get(ID_KEY) or "").strip()
    if lid:
        return ("id", lid)
    return ("nm",) + tuple(str(get(k) or "").strip() for k in UPSERT_KEYS)


def extract_sheet_id(url: str) -> str:
    m = SHEET_ID_RE.search(url)
    if not m:
        raise SystemExit(f"ERROR: could not extract sheet ID from CREDIT_LIMIT_SHEET_URL='{url}'")
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
    raise ValueError(f"Schema is missing required upsert key '{key}'")


def existing_key_to_row(existing: list[list[str]], columns: list[Column]) -> dict[tuple[str, ...], int]:
    """Return a map from upsert-key tuple to 1-based sheet row number.

    Skips data rows whose key is entirely blank (the sheet may have stray
    blank rows). The first data row is at row 2 (row 1 = header).
    """
    if len(existing) <= 1:
        return {}
    headers = [c.strip() for c in existing[0]]
    try:
        idx_map = {k: headers.index(_header_for(columns, k)) for k in (ID_KEY, *UPSERT_KEYS)}
    except ValueError as exc:
        raise SystemExit(f"ERROR: upsert column missing from sheet headers: {exc}")

    out: dict[tuple[str, ...], int] = {}
    for offset, row in enumerate(existing[1:], start=2):  # start=2 → 1-based sheet row
        def _get(k: str, _row: list[str] = row) -> str:
            i = idx_map[k]
            return _row[i] if i < len(_row) else ""
        key = _upsert_key(_get)
        # Skip fully-blank rows (no GUID and no name triplet).
        if key == ("nm", "", "", ""):
            continue
        out[key] = offset
    return out


def _normalize(v: Any) -> str:
    """Normalize a cell value for equality comparison.

    Sheets writes values with ``USER_ENTERED`` and parses numeric strings as
    numbers, so ``"800000.00"`` round-trips back to us as ``"800000"``. To
    avoid every numeric column reporting as "updated" on a no-op re-run, try
    a float parse first; fall back to a stripped string.
    """
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


def main() -> int:
    parser = argparse.ArgumentParser(description="Upsert Tally credit-limit JSON into Google Sheets.")
    parser.add_argument("--input", required=True, help="Path to JSON file produced by fetch_tally_credit_limits.py")
    args = parser.parse_args()

    load_dotenv()
    sheet_url = os.environ["CREDIT_LIMIT_SHEET_URL"]
    tab = os.environ.get("CREDIT_LIMIT_SHEET_TAB", "Sheet1")
    sheet_id = extract_sheet_id(sheet_url)

    columns = load_columns()
    rows_in: list[dict[str, Any]] = json.loads(Path(args.input).read_text(encoding="utf-8"))

    svc = authorize()
    existing = get_existing_rows(svc, sheet_id, tab)
    ensure_header(svc, sheet_id, tab, columns, existing)
    if not existing or all(c == "" for c in (existing[0] if existing else [])):
        existing = get_existing_rows(svc, sheet_id, tab)

    key_to_sheet_row = existing_key_to_row(existing, columns)

    # Last column letter for the schema-managed range — anything to the right
    # of this in the sheet is preserved.
    last_col_letter = _col_letter(len(columns) - 1)

    update_data: list[dict[str, Any]] = []
    new_rows: list[list[Any]] = []
    appended = 0
    updated = 0
    unchanged = 0

    for r in rows_in:
        key = _upsert_key(lambda k: r.get(k, ""))
        new_values = [r.get(c.key, "") for c in columns]
        if key in key_to_sheet_row:
            sheet_row = key_to_sheet_row[key]
            existing_values = existing[sheet_row - 1] if sheet_row - 1 < len(existing) else []
            existing_trimmed = [
                (existing_values[i] if i < len(existing_values) else "")
                for i in range(len(columns))
            ]
            # Normalize numeric cells so "800000" == "800000.00" — Sheets
            # rewrites our written floats as bare integers when the fraction
            # is zero, which would otherwise make every numeric column read
            # as "updated" on every no-op re-run.
            if [_normalize(v) for v in existing_trimmed] == [_normalize(v) for v in new_values]:
                unchanged += 1
                continue
            update_data.append({
                "range": f"'{tab}'!A{sheet_row}:{last_col_letter}{sheet_row}",
                "values": [new_values],
            })
            updated += 1
        else:
            new_rows.append(new_values)
            appended += 1

    if update_data:
        # batchUpdate handles many ranges in one call — much faster than
        # per-row updates when hundreds of ledgers change.
        svc.spreadsheets().values().batchUpdate(
            spreadsheetId=sheet_id,
            body={"valueInputOption": "USER_ENTERED", "data": update_data},
        ).execute()

    if new_rows:
        svc.spreadsheets().values().append(
            spreadsheetId=sheet_id,
            range=f"'{tab}'!A1",
            valueInputOption="USER_ENTERED",
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
