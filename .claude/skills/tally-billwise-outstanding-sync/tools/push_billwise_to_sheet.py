"""Push bill-wise outstanding JSON into the Google Sheet (SNAPSHOT replace).

Bill-wise is a snapshot: each sync the open-bill set changes (paid bills vanish,
new bills appear). So this is NOT an upsert — it is **replace-by-scope**: for
every (Company, Location) present in the input, all existing rows for that scope
are dropped and the fresh snapshot is written. Rows for scopes NOT in the input
are preserved (so a per-company push doesn't wipe other companies). created_at is
preserved per (company, location, ledger_id, bill_ref_name); updated_at bumps
whenever a bill's data changes.

Schema = reference/columns.md (parsed via _schema.py). Env:
    TALLY_BILLWISE_SHEET_URL   destination spreadsheet
    TALLY_BILLWISE_SHEET_TAB   tab name (default Sheet1)

Usage (from this tools/ dir):
    python push_billwise_to_sheet.py --input <fetch.json>
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _schema import load_columns  # noqa: E402

SCOPE_KEYS = ("company", "location")
IDENTITY_KEYS = ("company", "location", "ledger_id", "bill_ref_name")
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
BASE_DIR = Path(__file__).resolve().parents[4]  # project root (…/FETCH DAILY DATA)
CREDENTIALS_PATH = BASE_DIR / "credentials.json"
TOKEN_PATH = BASE_DIR / "token.json"


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def extract_sheet_id(url: str) -> str:
    m = re.search(r"/spreadsheets/d/([a-zA-Z0-9_-]+)", url)
    if not m:
        raise SystemExit(f"ERROR: cannot parse spreadsheet ID from {url}")
    return m.group(1)


def authorize() -> Any:
    creds = None
    if TOKEN_PATH.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(str(CREDENTIALS_PATH), SCOPES)
            creds = flow.run_local_server(port=0)
        TOKEN_PATH.write_text(creds.to_json(), encoding="utf-8")
    return build("sheets", "v4", credentials=creds)


def _norm(v: Any) -> str:
    s = "" if v is None else str(v).strip()
    # numeric-insensitive compare for amounts ("660" vs "660.00")
    try:
        return f"{float(s.replace(',', '')):.2f}"
    except (ValueError, AttributeError):
        return s


def main() -> int:
    ap = argparse.ArgumentParser(description="Snapshot-replace bill-wise JSON into Google Sheets.")
    ap.add_argument("--input", required=True)
    args = ap.parse_args()

    load_dotenv()
    sheet_url = os.environ["TALLY_BILLWISE_SHEET_URL"]
    tab = os.environ.get("TALLY_BILLWISE_SHEET_TAB", "Sheet1")
    sheet_id = extract_sheet_id(sheet_url)
    columns = load_columns()
    headers = [c.header for c in columns]
    keys = [c.key for c in columns]
    created_idx = keys.index("created_at") if "created_at" in keys else -1
    updated_idx = keys.index("updated_at") if "updated_at" in keys else -1

    rows_in: list[dict[str, Any]] = json.loads(Path(args.input).read_text(encoding="utf-8"))

    svc = authorize()
    rng = f"'{tab}'!A1:ZZ"
    existing = svc.spreadsheets().values().get(spreadsheetId=sheet_id, range=rng).execute().get("values", [])
    has_header = bool(existing) and [h.strip() for h in existing[0][:len(headers)]] == headers
    body_rows = existing[1:] if has_header else (existing if existing else [])

    # index existing by header position
    hidx = {h: i for i, h in enumerate(existing[0])} if has_header else {h: i for i, h in enumerate(headers)}

    def cell(row, header):
        i = hidx.get(header, -1)
        return row[i] if 0 <= i < len(row) else ""

    # created_at preservation map
    created_map: dict[tuple, str] = {}
    for row in body_rows:
        idkey = tuple(cell(row, dict(zip(keys, headers))[k]).strip() for k in IDENTITY_KEYS)
        ca = cell(row, "created_at").strip()
        if ca:
            created_map[idkey] = ca

    # scopes being replaced
    in_scopes = {tuple(str(r.get(k, "")).strip() for k in SCOPE_KEYS) for r in rows_in}

    # keep existing rows whose scope is NOT in the input
    kept: list[list[str]] = []
    for row in body_rows:
        scope = tuple(cell(row, dict(zip(keys, headers))[k]).strip() for k in SCOPE_KEYS)
        if scope not in in_scopes:
            kept.append([cell(row, h) for h in headers])

    run_ts = now_iso()
    new_rows: list[list[str]] = []
    for r in rows_in:
        vals = [str(r.get(k, "")) for k in keys]
        idkey = tuple(str(r.get(k, "")).strip() for k in IDENTITY_KEYS)
        if created_idx >= 0:
            vals[created_idx] = created_map.get(idkey, run_ts)
        if updated_idx >= 0:
            vals[updated_idx] = run_ts
        new_rows.append(vals)

    final = [headers] + kept + new_rows
    # clear + rewrite the whole data range
    svc.spreadsheets().values().clear(spreadsheetId=sheet_id, range=rng).execute()
    svc.spreadsheets().values().update(
        spreadsheetId=sheet_id, range=f"'{tab}'!A1",
        valueInputOption="RAW", body={"values": final},
    ).execute()

    summary = {"fetched": len(rows_in), "scopes_replaced": [list(s) for s in sorted(in_scopes)],
               "kept_other_scope_rows": len(kept), "written_rows": len(new_rows),
               "total_rows": len(new_rows) + len(kept), "sheet_url": sheet_url}
    print(json.dumps(summary))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
