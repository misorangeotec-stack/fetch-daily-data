"""Append fetched sales rows to **two** Google Sheets in one run:

1. **Sales sheet** (per-voucher summary) — `SALES_SHEET_URL` / `SALES_SHEET_TAB`,
   schema from ``reference/columns.md``, dedupe ``(Company, Location, Voucher No.)``.
2. **Sales Outstanding Register** (bill-wise detail) — `SALES_DETAILS_SHEET_URL` /
   `SALES_DETAILS_SHEET_TAB`, schema from ``reference/columns_details.md``,
   dedupe ``(Company, Location, Voucher No., Bill Ref Name)``.

The two sheets join on ``(Company, Location, Voucher No.)``. Both pushes are
idempotent — re-running with overlapping date ranges only appends new rows.

Reads the JSON written by ``fetch_tally_sales.py``. Accepts both shapes:

* New: ``{"vouchers": [...], "details": [...]}``
* Legacy: ``[...]`` (treated as ``vouchers`` only; details push is skipped
  with a warning).

Required env vars (loaded from project .env):
    GOOGLE_CREDENTIALS_FILE     path to OAuth client secrets JSON
    GOOGLE_TOKEN_FILE           path to cached OAuth token JSON
    SALES_SHEET_URL             URL of the per-voucher Sales sheet
    SALES_SHEET_TAB             tab name within that sheet (default: "Sales")
    SALES_DETAILS_SHEET_URL     URL of the bill-wise detail sheet
    SALES_DETAILS_SHEET_TAB     tab name within that sheet
                                (default: "Sales Outstanding Register")

Usage:
    python push_sales_to_sheet.py --input .tmp/sales_<ts>.json
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
from _schema import Column, load_columns, load_details_columns  # noqa: E402


SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
SHEET_ID_RE = re.compile(r"/spreadsheets/d/([a-zA-Z0-9-_]+)")

# Dedupe keys per sheet. Composite tuples — every element is the value of the
# corresponding sheet column, stripped.
#
# ``date`` is in the key because some Tally voucher types reset their
# auto-numbering counter at the FY rollover but keep the FY prefix
# hard-coded (e.g. "HD/N/25-26/1" appears in both Apr 2025 and Apr 2026 with
# different parties / amounts — genuinely different vouchers reusing the
# same number). Without ``date`` in the dedupe key, the second voucher
# would be dropped as a "duplicate" when re-syncing across years.
DEDUPE_KEYS_VOUCHERS = ("company", "location", "voucher_no", "date")
DEDUPE_KEYS_DETAILS = ("company", "location", "voucher_no", "bill_ref_name", "date")
DATE_HEADER = "Date"


def extract_sheet_id(url: str, var_name: str) -> str:
    m = SHEET_ID_RE.search(url)
    if not m:
        raise SystemExit(f"ERROR: could not extract sheet ID from {var_name}='{url}'")
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
    # Per-request HTTP timeout so a stalled Sheets API call fails fast instead of hanging forever
    # (httplib2 has no default timeout — the root cause of the self-heal hang, 2026-06-12). 120s is
    # well above normal call latency; a read that stalls raises socket.timeout, leaving the tab
    # untouched, rather than wedging the whole heal.
    import httplib2
    from google_auth_httplib2 import AuthorizedHttp
    authed_http = AuthorizedHttp(creds, http=httplib2.Http(timeout=120))
    return build("sheets", "v4", http=authed_http, cache_discovery=False)


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
        # Empty tab — write header.
        _write_header(svc, sheet_id, tab, expected)
        return
    actual = [c.strip() for c in existing[0]]
    actual_trimmed = actual[: len(expected)]
    if all(c == "" for c in actual):
        _write_header(svc, sheet_id, tab, expected)
        return
    if actual_trimmed != expected:
        raise SystemExit(
            f"ERROR: sheet '{tab}' header row does not match the schema.\n"
            f"  Expected: {expected}\n"
            f"  Found:    {actual}\n"
            "Either fix the sheet's row 1 to match, or update the corresponding columns*.md."
        )


def _write_header(svc: Any, sheet_id: str, tab: str, headers: list[str]) -> None:
    svc.spreadsheets().values().update(
        spreadsheetId=sheet_id,
        range=f"'{tab}'!A1",
        valueInputOption="RAW",
        body={"values": [headers]},
    ).execute()


def existing_keys(existing: list[list[str]], columns: list[Column], dedupe_keys: tuple[str, ...]) -> set[tuple[str, ...]]:
    """Build the set of dedupe-key tuples already present in the sheet."""
    if len(existing) <= 1:
        return set()
    headers = [c.strip() for c in existing[0]]
    try:
        col_indices = [headers.index(_header_for(columns, k)) for k in dedupe_keys]
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
    svc.spreadsheets().values().append(
        spreadsheetId=sheet_id,
        range=f"'{tab}'!A1",
        valueInputOption="RAW",
        insertDataOption="INSERT_ROWS",
        body={"values": rows},
    ).execute()


def _grid_id(svc: Any, sheet_id: str, tab: str) -> int:
    meta = svc.spreadsheets().get(spreadsheetId=sheet_id, fields="sheets.properties").execute()
    for s in meta.get("sheets", []):
        p = s.get("properties", {})
        if p.get("title") == tab:
            return int(p.get("sheetId"))
    raise SystemExit(f"ERROR: tab '{tab}' not found for upsert row delete")


def push_to_sheet(
    svc: Any,
    sheet_url: str,
    tab: str,
    columns: list[Column],
    rows_in: list[dict[str, Any]],
    dedupe_keys: tuple[str, ...],
    var_name: str,
) -> dict[str, Any]:
    """Content-aware voucher UPSERT (default push). Groups rows by the voucher identity
    ``dedupe_keys[:3]`` = (company, location, voucher_no) — for the per-voucher Sales sheet that's
    one row per group; for the bill-wise Register it's all of a voucher's detail rows. For each
    voucher in the batch it compares the FULL row-set to the sheet's: identical → skip; changed (any
    column — amount edit, a changed/added/removed bill allocation) → delete the sheet's rows for that
    voucher and re-append the fetched rows; new → plain append. So any Tally edit is reflected on the
    next covering sync (and this also de-dupes any historic Register row-bloat), while unchanged
    vouchers cost nothing and a forward sync of all-new vouchers is a plain append.

    ``var_name`` is the env-var label used in error messages so the user knows which sheet failed.
    """
    sheet_id = extract_sheet_id(sheet_url, var_name)
    existing = get_existing_rows(svc, sheet_id, tab)
    ensure_header(svc, sheet_id, tab, columns, existing)
    if not existing or all(c == "" for c in (existing[0] if existing else [])):
        existing = get_existing_rows(svc, sheet_id, tab)

    group_keys = dedupe_keys[:3]    # (company, location, voucher_no) — the voucher identity
    ncol = len(columns)
    batch_groups: dict[tuple[str, ...], list[list[Any]]] = {}
    for r in rows_in:
        gk = tuple(str(r.get(k, "")).strip() for k in group_keys)
        batch_groups.setdefault(gk, []).append([r.get(c.key, "") for c in columns])

    existing_groups: dict[tuple[str, ...], list[tuple[int, tuple[str, ...]]]] = {}
    if existing and len(existing) > 1:
        headers = [h.strip() for h in existing[0]]
        try:
            gk_idx = [headers.index(_header_for(columns, k)) for k in group_keys]
        except ValueError as exc:
            raise SystemExit(f"ERROR: upsert key column missing from sheet headers: {exc}")
        for gi, row in enumerate(existing[1:], start=1):
            gk = tuple(row[gk_idx[j]].strip() if gk_idx[j] < len(row) else "" for j in range(len(gk_idx)))
            content = tuple((row[i].strip() if i < len(row) else "") for i in range(ncol))
            existing_groups.setdefault(gk, []).append((gi, content))

    to_append: list[list[Any]] = []
    to_delete: list[int] = []
    for gk, brows in batch_groups.items():
        ex = existing_groups.get(gk)
        batch_norm = sorted(tuple(str(x).strip() for x in r) for r in brows)
        if ex is not None and sorted(t for (_, t) in ex) == batch_norm:
            continue                                  # unchanged voucher → leave untouched (no rewrite)
        if ex:
            to_delete.extend(gi for (gi, _) in ex)    # changed → drop the stale rows for this voucher
        to_append.extend(brows)

    append_rows(svc, sheet_id, tab, to_append)
    if to_delete:
        grid_id = _grid_id(svc, sheet_id, tab)
        ranges: list[list[int]] = []
        for gi in sorted(to_delete):
            if ranges and gi == ranges[-1][1]:
                ranges[-1][1] = gi + 1
            else:
                ranges.append([gi, gi + 1])
        reqs = [{"deleteDimension": {"range": {"sheetId": grid_id, "dimension": "ROWS",
                 "startIndex": s, "endIndex": e}}} for s, e in sorted(ranges, reverse=True)]
        svc.spreadsheets().batchUpdate(spreadsheetId=sheet_id, body={"requests": reqs}).execute()

    return {
        "fetched": len(rows_in),
        "appended": len(to_append),
        "deleted": len(to_delete),
        "skipped": len(rows_in) - len(to_append),
        "sheet_url": sheet_url,
        "tab": tab,
    }


def _split_input(raw: Any) -> tuple[list[dict[str, Any]], list[dict[str, Any]] | None]:
    """Return ``(voucher_rows, detail_rows_or_None)``.

    Accepts both the new dict shape and the legacy flat list (treated as
    voucher rows only — details push is skipped).
    """
    if isinstance(raw, list):
        return raw, None
    if isinstance(raw, dict):
        return raw.get("vouchers", []), raw.get("details", [])
    raise SystemExit(f"ERROR: unexpected input JSON shape: {type(raw).__name__}")


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
    # Data-driven scope: delete only the (Company, Location) pairs we actually have
    # replacement data for in new_rows — derived from the data itself, not the CLI arg.
    # The sheet stores Company and Location in separate columns, so filtering by Company
    # alone would wipe OTHER locations of the same Company (e.g. an O-tec/Surat push
    # deleting O-tec/Noida rows). Deriving scope from new_rows also makes an empty fetch
    # a no-op instead of a destructive wipe. See RECONCILIATION_NOTES.md EC-11.
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
    fetched rows for that one party). Applied to BOTH the per-voucher Sales sheet and the
    bill-wise Sales Outstanding Register (each carries ``ledger_id`` + ``Date``).

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
    parser = argparse.ArgumentParser(description="Push fetched sales JSON to Google Sheets (vouchers + bill-wise details).")
    parser.add_argument("--input", required=True, help="Path to JSON file produced by fetch_tally_sales.py")
    parser.add_argument("--reconcile", action="store_true", help="Replace-in-range: delete stale rows and re-insert from Tally.")
    parser.add_argument("--from", dest="from_date", default=None, help="DD-MM-YYYY range start (required with --reconcile)")
    parser.add_argument("--to", dest="to_date", default=None, help="DD-MM-YYYY range end (required with --reconcile)")
    parser.add_argument("--companies", default=None, help="Company name (required with --reconcile)")
    parser.add_argument("--heal", action="store_true",
                        help="GUID-keyed surgical heal: delete ONLY --ledger-id's rows in [--from,--to] (on BOTH the Sales sheet and the Register) and re-insert that party's fetched rows.")
    parser.add_argument("--ledger-id", dest="ledger_id", default=None,
                        help="Tally ledger GUID to heal (required with --heal). Only this party's rows are touched.")
    parser.add_argument("--backup-dir", dest="backup_dir", default=None,
                        help="Directory for the pre-heal tab backups (default: backups/heal_<ts>). Pass one shared dir to group a heal session's backups.")
    args = parser.parse_args()

    load_dotenv()
    sales_url = os.environ["SALES_SHEET_URL"]
    sales_tab = os.environ.get("SALES_SHEET_TAB", "Sales")

    voucher_columns = load_columns()
    detail_columns = load_details_columns()
    raw = json.loads(Path(args.input).read_text(encoding="utf-8"))
    voucher_rows, detail_rows = _split_input(raw)

    svc = authorize()

    if args.heal:
        if not args.ledger_id or not args.from_date or not args.to_date:
            raise SystemExit("ERROR: --heal requires --ledger-id, --from, and --to")
        from_d = datetime.strptime(args.from_date, "%d-%m-%Y").date()
        to_d   = datetime.strptime(args.to_date,   "%d-%m-%Y").date()
        target = args.ledger_id.strip()
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_dir = args.backup_dir or str(Path("backups") / f"heal_{ts}")

        # ── Sales (per-voucher) sheet ──────────────────────────────────────────
        sales_id = extract_sheet_id(sales_url, "SALES_SHEET_URL")
        existing_v = get_existing_rows(svc, sales_id, sales_tab)
        ensure_header(svc, sales_id, sales_tab, voucher_columns, existing_v)
        _backup_tab(svc, sales_id, sales_tab, backup_dir, f"sales_preheal_{target[-12:]}")
        seen_v: set[tuple[str, ...]] = set()
        batch_v: list[list[Any]] = []
        for r in voucher_rows:
            if str(r.get("ledger_id", "")).strip() != target:
                continue
            key = tuple(str(r.get(k, "")).strip() for k in DEDUPE_KEYS_VOUCHERS)
            if key not in seen_v:
                seen_v.add(key)
                batch_v.append([r.get(c.key, "") for c in voucher_columns])
        vstats = _heal_sheet(svc, sales_id, sales_tab, batch_v, voucher_columns, target, from_d, to_d)

        # ── Sales Outstanding Register (bill-wise detail) sheet ────────────────
        details_url = os.environ.get("SALES_DETAILS_SHEET_URL", "").strip()
        details_tab = os.environ.get("SALES_DETAILS_SHEET_TAB", "Sales Outstanding Register")
        dstats: dict[str, int] = {"inserted": 0, "deleted": 0}
        if details_url and detail_rows is not None:
            details_id = extract_sheet_id(details_url, "SALES_DETAILS_SHEET_URL")
            existing_d = get_existing_rows(svc, details_id, details_tab)
            ensure_header(svc, details_id, details_tab, detail_columns, existing_d)
            _backup_tab(svc, details_id, details_tab, backup_dir, f"register_preheal_{target[-12:]}")
            seen_d: set[tuple[str, ...]] = set()
            batch_d: list[list[Any]] = []
            for r in detail_rows:
                if str(r.get("ledger_id", "")).strip() != target:
                    continue
                key = tuple(str(r.get(k, "")).strip() for k in DEDUPE_KEYS_DETAILS)
                if key not in seen_d:
                    seen_d.add(key)
                    batch_d.append([r.get(c.key, "") for c in detail_columns])
            dstats = _heal_sheet(svc, details_id, details_tab, batch_d, detail_columns, target, from_d, to_d)

        summary = {
            "mode": "heal",
            "ledger_id": target,
            "fetched": len(voucher_rows),
            "appended": vstats["inserted"],
            "deleted": vstats["deleted"],
            "skipped": 0,
            "backup_dir": backup_dir,
            "sheet_url": sales_url,
            "details": {
                "fetched": len(detail_rows) if detail_rows is not None else 0,
                "appended": dstats["inserted"],
                "deleted": dstats["deleted"],
                "skipped": 0,
            },
        }
        print(json.dumps(summary))
        return 0

    if args.reconcile:
        if not args.from_date or not args.to_date or not args.companies:
            raise SystemExit("ERROR: --reconcile requires --from, --to, and --companies")
        from_d = datetime.strptime(args.from_date, "%d-%m-%Y").date()
        to_d   = datetime.strptime(args.to_date,   "%d-%m-%Y").date()
        sales_id = extract_sheet_id(sales_url, "SALES_SHEET_URL")
        existing_v = get_existing_rows(svc, sales_id, sales_tab)
        ensure_header(svc, sales_id, sales_tab, voucher_columns, existing_v)
        seen_batch_v: set[tuple[str, ...]] = set()
        batch_v: list[list[Any]] = []
        for r in voucher_rows:
            key = tuple(str(r.get(k, "")).strip() for k in DEDUPE_KEYS_VOUCHERS)
            if key not in seen_batch_v:
                seen_batch_v.add(key)
                batch_v.append([r.get(c.key, "") for c in voucher_columns])
        vstats = _reconcile_sheet(svc, sales_id, sales_tab, batch_v, voucher_columns, args.companies, from_d, to_d)
        details_url = os.environ.get("SALES_DETAILS_SHEET_URL", "").strip()
        details_tab = os.environ.get("SALES_DETAILS_SHEET_TAB", "Sales Outstanding Register")
        dstats: dict[str, int] = {"inserted": 0, "deleted": 0}
        if details_url and detail_rows is not None:
            details_id = extract_sheet_id(details_url, "SALES_DETAILS_SHEET_URL")
            existing_d = get_existing_rows(svc, details_id, details_tab)
            ensure_header(svc, details_id, details_tab, detail_columns, existing_d)
            seen_batch_d: set[tuple[str, ...]] = set()
            batch_d: list[list[Any]] = []
            for r in detail_rows:
                key = tuple(str(r.get(k, "")).strip() for k in DEDUPE_KEYS_DETAILS)
                if key not in seen_batch_d:
                    seen_batch_d.add(key)
                    batch_d.append([r.get(c.key, "") for c in detail_columns])
            dstats = _reconcile_sheet(svc, details_id, details_tab, batch_d, detail_columns, args.companies, from_d, to_d)
        summary = {
            "fetched": len(voucher_rows),
            "appended": vstats["inserted"],
            "deleted": vstats["deleted"],
            "skipped": 0,
            "sheet_url": sales_url,
            "details": {
                "fetched": len(detail_rows) if detail_rows is not None else 0,
                "appended": dstats["inserted"],
                "deleted": dstats["deleted"],
                "skipped": 0,
            },
        }
        print(json.dumps(summary))
        return 0

    voucher_summary = push_to_sheet(
        svc, sales_url, sales_tab, voucher_columns,
        voucher_rows, DEDUPE_KEYS_VOUCHERS, "SALES_SHEET_URL",
    )

    if detail_rows is None:
        # Legacy flat-list input — no details to push. Surface this clearly so
        # the operator knows to re-fetch with the new fetch script if they
        # want the detail sheet populated.
        print(
            "WARNING: input JSON is in the legacy flat-list shape — skipping "
            "the bill-wise detail push. Re-fetch with the current "
            "fetch_tally_sales.py to populate the Sales Outstanding Register.",
            file=sys.stderr,
        )
        details_summary = {"fetched": 0, "appended": 0, "skipped": 0, "skipped_legacy": True}
    else:
        details_url = os.environ.get("SALES_DETAILS_SHEET_URL", "").strip()
        details_tab = os.environ.get("SALES_DETAILS_SHEET_TAB", "Sales Outstanding Register")
        if not details_url:
            print(
                "WARNING: SALES_DETAILS_SHEET_URL is not set — skipping the "
                "bill-wise detail push. Add it to .env to populate the Sales "
                "Outstanding Register.",
                file=sys.stderr,
            )
            details_summary = {"fetched": len(detail_rows), "appended": 0, "skipped": 0, "skipped_no_env": True}
        else:
            details_summary = push_to_sheet(
                svc, details_url, details_tab, detail_columns,
                detail_rows, DEDUPE_KEYS_DETAILS, "SALES_DETAILS_SHEET_URL",
            )

    summary = {
        "mode": "upsert",
        "fetched": voucher_summary["fetched"],
        "appended": voucher_summary["appended"],
        "deleted": voucher_summary.get("deleted", 0),
        "skipped": voucher_summary["skipped"],
        "sheet_url": voucher_summary["sheet_url"],
        "details": details_summary,
    }
    print(json.dumps(summary))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
