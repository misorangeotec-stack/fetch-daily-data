"""Append fetched debit-note rows to the outstanding-report Google Sheet.

Validates the destination tab's row 1 against the schema in
``reference/columns.md``, dedupes by composite ``(Company, Location, Voucher No.)``
against rows already present, and appends only the new ones.

Required env vars (loaded from project .env):
    GOOGLE_CREDENTIALS_FILE    path to OAuth client secrets JSON
    GOOGLE_TOKEN_FILE          path to cached OAuth token JSON
    DEBIT_NOTE_SHEET_URL       full URL of the destination Google Sheet
    DEBIT_NOTE_SHEET_TAB       tab/sheet name within the spreadsheet (default: Sheet1)

Usage:
    python push_debit_notes_to_sheet.py --input .tmp/debit_notes_<ts>.json
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
from datetime import date, datetime
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
DEDUPE_KEYS = ("company", "location", "voucher_no")
DATE_HEADER = "Date"


def extract_sheet_id(url: str) -> str:
    m = SHEET_ID_RE.search(url)
    if not m:
        raise SystemExit(f"ERROR: could not extract sheet ID from DEBIT_NOTE_SHEET_URL='{url}'")
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


def existing_keys(existing: list[list[str]], columns: list[Column]) -> set[tuple[str, ...]]:
    if len(existing) <= 1:
        return set()
    headers = [c.strip() for c in existing[0]]
    try:
        col_indices = [headers.index(_header_for(columns, k)) for k in DEDUPE_KEYS]
    except ValueError as exc:
        raise SystemExit(f"ERROR: dedupe column missing from sheet headers: {exc}")

    keys: set[tuple[str, ...]] = set()
    for row in existing[1:]:
        key = tuple(row[i].strip() if i < len(row) else "" for i in col_indices)
        if any(key):
            keys.add(key)
    return keys


def _header_for(columns: list[Column], key: str) -> str:
    for c in columns:
        if c.key == key:
            return c.header
    raise ValueError(f"Schema is missing required dedupe key '{key}'")


def append_rows(svc: Any, sheet_id: str, tab: str, rows: list[list[Any]]) -> None:
    if not rows:
        return
    # RAW per project convention (CLAUDE.md): USER_ENTERED corrupts phone-
    # number strings and rounds large numbers on round-trip.
    svc.spreadsheets().values().append(
        spreadsheetId=sheet_id,
        range=f"'{tab}'!A1",
        valueInputOption="RAW",
        insertDataOption="INSERT_ROWS",
        body={"values": rows},
    ).execute()


def _parse_date(s: str) -> date | None:
    for fmt in ("%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y"):
        try:
            return datetime.strptime(s.strip(), fmt).date()
        except ValueError:
            pass
    return None


def _clear_and_write(svc: Any, sheet_id: str, tab: str, rows: list[list]) -> None:
    svc.spreadsheets().values().clear(
        spreadsheetId=sheet_id, range=f"'{tab}'"
    ).execute()
    if rows:
        svc.spreadsheets().values().update(
            spreadsheetId=sheet_id,
            range=f"'{tab}'!A1",
            valueInputOption="RAW",
            body={"values": rows},
        ).execute()


def _reconcile_sheet(
    svc: Any,
    sheet_id: str,
    tab: str,
    new_rows: list[list[Any]],
    columns: list[Column],
    company: str,
    from_d: date,
    to_d: date,
) -> dict[str, int]:
    existing = get_existing_rows(svc, sheet_id, tab)
    header = [c.header for c in columns]
    if not existing or all(c == "" for c in existing[0]):
        _clear_and_write(svc, sheet_id, tab, [header] + new_rows)
        return {"inserted": len(new_rows), "deleted": 0}
    curr_header = existing[0]
    lower = [h.strip().lower() for h in curr_header]
    try:
        date_idx = lower.index(DATE_HEADER.lower())
        co_idx   = lower.index("company")
    except ValueError as e:
        raise SystemExit(f"ERROR: required column missing in sheet: {e}")
    loc_idx = lower.index("location") if "location" in lower else None
    # Data-driven scope: delete only the (Company, Location) pairs present in new_rows
    # (DISPLAY names, derived from the data), within the window — NOT by the raw
    # --companies arg (which is the Tally name, never matches the sheet's display name)
    # and NOT by Company alone (would wipe other locations). Empty fetch = no-op.
    # See RECONCILIATION_NOTES.md EC-11 (ported from push_sales_to_sheet.py).
    key_cols = [c.key for c in columns]
    nci = key_cols.index("company") if "company" in key_cols else None
    nli = key_cols.index("location") if "location" in key_cols else None
    scope = set()
    for r in new_rows:
        rc = str(r[nci]).strip() if nci is not None and nci < len(r) else ""
        rl = str(r[nli]).strip() if nli is not None and nli < len(r) else ""
        scope.add((rc, rl))
    out_of_scope = []
    in_scope_count = 0
    for row in existing[1:]:
        row_co   = row[co_idx].strip() if co_idx < len(row) else ""
        row_loc  = row[loc_idx].strip() if (loc_idx is not None and loc_idx < len(row)) else ""
        date_str = row[date_idx].strip() if date_idx < len(row) else ""
        row_date = _parse_date(date_str)
        if (row_co, row_loc) in scope and row_date and from_d <= row_date <= to_d:
            in_scope_count += 1
        else:
            out_of_scope.append(row)
    _clear_and_write(svc, sheet_id, tab, [curr_header] + out_of_scope + new_rows)
    return {"inserted": len(new_rows), "deleted": in_scope_count}


def _backup_tab(svc: Any, sheet_id: str, tab: str, backup_dir: str, label: str) -> tuple[str, int]:
    """Snapshot a tab to <backup_dir>/<label>.csv before a destructive heal write (EC-10).

    Returns (path, data_row_count). Always taken FIRST so a heal can be rolled back by
    re-uploading the CSV. Pass one shared backup_dir to group a whole heal session's snapshots.
    """
    rows = get_existing_rows(svc, sheet_id, tab)
    bdir = Path(backup_dir)
    bdir.mkdir(parents=True, exist_ok=True)
    out = bdir / f"{label}.csv"
    with out.open("w", newline="", encoding="utf-8") as f:
        csv.writer(f).writerows(rows)
    return str(out), max(0, len(rows) - 1)


def _heal_sheet(
    svc: Any,
    sheet_id: str,
    tab: str,
    new_rows: list[list[Any]],
    columns: list[Column],
    ledger_id: str,
    from_d: date,
    to_d: date,
) -> dict[str, int]:
    """GUID-keyed surgical replace-in-range (heal): delete ONLY ``ledger_id``'s rows whose date
    is within [from_d, to_d], keep everything else, then re-insert ``new_rows`` (the freshly-
    fetched rows for that one party).

    A Tally GUID is globally unique to one ledger in one (Company, Location), so matching on
    ``ledger_id`` alone is sufficient and correct — no (Company, Location) scope juggling, and no
    risk of touching another party. Unlike the company-wide ``_reconcile_sheet``, an EMPTY
    ``new_rows`` here is intentional and SAFE: it deletes only this one party's stale in-window
    rows (e.g. a voucher removed in Tally), never a company wipe. See RECONCILE_HEAL_PLAN.md P1
    and RECONCILIATION_NOTES.md EC-10/EC-11.
    """
    ledger_id = str(ledger_id or "").strip()
    if not ledger_id:
        raise SystemExit("ERROR: heal requires a non-empty ledger_id (blank would match every unstamped row).")
    existing = get_existing_rows(svc, sheet_id, tab)
    header = [c.header for c in columns]
    if not existing or all(c == "" for c in existing[0]):
        _clear_and_write(svc, sheet_id, tab, [header] + new_rows)
        return {"inserted": len(new_rows), "deleted": 0}
    curr_header = existing[0]
    lower = [h.strip().lower() for h in curr_header]
    try:
        date_idx = lower.index(DATE_HEADER.lower())
    except ValueError as e:
        raise SystemExit(f"ERROR: required '{DATE_HEADER}' column missing in sheet '{tab}': {e}")
    if "ledger_id" not in lower:
        raise SystemExit(
            f"ERROR: sheet '{tab}' has no ledger_id column — cannot heal by GUID. "
            "Run the Phase-C ledger_id migration for this sheet first."
        )
    lid_idx = lower.index("ledger_id")
    kept: list[list[Any]] = []
    deleted = 0
    for row in existing[1:]:
        row_lid  = row[lid_idx].strip() if lid_idx < len(row) else ""
        date_str = row[date_idx].strip() if date_idx < len(row) else ""
        row_date = _parse_date(date_str)
        if row_lid == ledger_id and row_date and from_d <= row_date <= to_d:
            deleted += 1
        else:
            kept.append(row)
    _clear_and_write(svc, sheet_id, tab, [curr_header] + kept + new_rows)
    return {"inserted": len(new_rows), "deleted": deleted}


def main() -> int:
    parser = argparse.ArgumentParser(description="Push fetched debit-note JSON to Google Sheets.")
    parser.add_argument("--input", required=True, help="Path to JSON file produced by fetch_tally_debit_notes.py")
    parser.add_argument("--reconcile", action="store_true", help="Replace-in-range: delete stale rows and re-insert from Tally.")
    parser.add_argument("--from", dest="from_date", default=None, help="DD-MM-YYYY range start (required with --reconcile)")
    parser.add_argument("--to", dest="to_date", default=None, help="DD-MM-YYYY range end (required with --reconcile)")
    parser.add_argument("--companies", default=None, help="Company name (required with --reconcile)")
    parser.add_argument("--heal", action="store_true",
                        help="GUID-keyed surgical heal: delete ONLY --ledger-id's rows in [--from,--to] and re-insert that party's fetched rows.")
    parser.add_argument("--ledger-id", dest="ledger_id", default=None,
                        help="Tally ledger GUID to heal (required with --heal). Only this party's rows are touched.")
    parser.add_argument("--backup-dir", dest="backup_dir", default=None,
                        help="Directory for the pre-heal tab backup (default: backups/heal_<ts>). Pass one shared dir to group a heal session's backups.")
    args = parser.parse_args()

    load_dotenv()
    sheet_url = os.environ["DEBIT_NOTE_SHEET_URL"]
    tab = os.environ.get("DEBIT_NOTE_SHEET_TAB", "Sheet1")
    sheet_id = extract_sheet_id(sheet_url)

    columns = load_columns()
    rows_in: list[dict[str, Any]] = json.loads(Path(args.input).read_text(encoding="utf-8"))

    svc = authorize()

    if args.heal:
        if not args.ledger_id or not args.from_date or not args.to_date:
            raise SystemExit("ERROR: --heal requires --ledger-id, --from, and --to")
        from_d = datetime.strptime(args.from_date, "%d-%m-%Y").date()
        to_d   = datetime.strptime(args.to_date,   "%d-%m-%Y").date()
        target = args.ledger_id.strip()
        existing = get_existing_rows(svc, sheet_id, tab)
        ensure_header(svc, sheet_id, tab, columns, existing)
        # Back up the tab before the destructive write (EC-10). One shared dir per heal
        # session if --backup-dir is passed; else a self-contained timestamped dir.
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_dir = args.backup_dir or str(Path("backups") / f"heal_{ts}")
        bpath, brows = _backup_tab(svc, sheet_id, tab, backup_dir, f"debitnote_preheal_{target[-12:]}")
        # Keep ONLY this party's rows from the fetch (defensive — P2 already filters to the GUID);
        # dedupe within the batch.
        seen_batch: set[tuple[str, ...]] = set()
        batch_rows: list[list[Any]] = []
        for r in rows_in:
            if str(r.get("ledger_id", "")).strip() != target:
                continue
            key = tuple(str(r.get(k, "")).strip() for k in DEDUPE_KEYS)
            if key not in seen_batch:
                seen_batch.add(key)
                batch_rows.append([r.get(c.key, "") for c in columns])
        stats = _heal_sheet(svc, sheet_id, tab, batch_rows, columns, target, from_d, to_d)
        summary = {
            "mode": "heal",
            "ledger_id": target,
            "fetched": len(rows_in),
            "appended": stats["inserted"],
            "deleted": stats["deleted"],
            "skipped": 0,
            "backup": bpath,
            "backup_rows": brows,
            "sheet_url": sheet_url,
        }
        print(json.dumps(summary))
        return 0

    if args.reconcile:
        if not args.from_date or not args.to_date or not args.companies:
            raise SystemExit("ERROR: --reconcile requires --from, --to, and --companies")
        from_d = datetime.strptime(args.from_date, "%d-%m-%Y").date()
        to_d   = datetime.strptime(args.to_date,   "%d-%m-%Y").date()
        existing = get_existing_rows(svc, sheet_id, tab)
        ensure_header(svc, sheet_id, tab, columns, existing)
        seen_batch: set[tuple[str, ...]] = set()
        batch_rows: list[list[Any]] = []
        for r in rows_in:
            key = tuple(str(r.get(k, "")).strip() for k in DEDUPE_KEYS)
            if key not in seen_batch:
                seen_batch.add(key)
                batch_rows.append([r.get(c.key, "") for c in columns])
        stats = _reconcile_sheet(svc, sheet_id, tab, batch_rows, columns, args.companies, from_d, to_d)
        summary = {
            "fetched": len(rows_in),
            "appended": stats["inserted"],
            "deleted": stats["deleted"],
            "skipped": 0,
            "sheet_url": sheet_url,
        }
        print(json.dumps(summary))
        return 0

    existing = get_existing_rows(svc, sheet_id, tab)
    ensure_header(svc, sheet_id, tab, columns, existing)
    if not existing or all(c == "" for c in (existing[0] if existing else [])):
        existing = get_existing_rows(svc, sheet_id, tab)

    seen = existing_keys(existing, columns)

    new_rows: list[list[Any]] = []
    skipped = 0
    for r in rows_in:
        key = tuple(str(r.get(k, "")).strip() for k in DEDUPE_KEYS)
        if key in seen:
            skipped += 1
            continue
        new_rows.append([r.get(c.key, "") for c in columns])

    append_rows(svc, sheet_id, tab, new_rows)

    summary = {
        "fetched": len(rows_in),
        "appended": len(new_rows),
        "skipped": skipped,
        "sheet_url": sheet_url,
    }
    print(json.dumps(summary))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
