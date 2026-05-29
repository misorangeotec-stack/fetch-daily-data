"""Cancellation audit: compare each Google Sheet against a live Tally fetch.

Rows whose (Company, Location, Voucher No.) key is present in the sheet
for the audit date range but absent from Tally's active-voucher list are
flagged — indicating the voucher was cancelled or deleted in Tally after
the last sync.

Usage (from Streamlit):
    from cancellation_audit import AuditRunner
    runner = AuditRunner(companies, masters, from_date, to_date)
    runner.start()
    while runner.is_running():
        for ev in runner.drain_events():
            ...  # ev["kind"] in {"progress", "master_done", "all_done", "error"}
    flagged = runner.results  # list[dict]
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import time
from collections import deque
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SKILLS_ROOT  = PROJECT_ROOT / ".claude" / "skills"
TMP_ROOT     = PROJECT_ROOT / ".tmp"

# master key → (skill_dir, fetch_script, sheet_url_env, sheet_tab_env, default_tab)
MASTER_CONFIG: dict[str, tuple[str, str, str, str, str]] = {
    "sales":           ("tally-sales-sync-outstanding",           "fetch_tally_sales.py",        "SALES_SHEET_URL",        "SALES_SHEET_TAB",        "Sheet1"),
    "salescreditnote": ("tally-salescreditnote-sync-outstanding",  "fetch_tally_credit_notes.py", "CREDIT_NOTE_SHEET_URL",  "CREDIT_NOTE_SHEET_TAB",  "Sheet1"),
    "salesdebitnote":  ("tally-salesdebitnote-sync-outstanding",   "fetch_tally_debit_notes.py",  "DEBIT_NOTE_SHEET_URL",   "DEBIT_NOTE_SHEET_TAB",   "Sheet1"),
    "salesjournal":    ("tally-salesjournal-sync-outstanding",     "fetch_tally_journals.py",     "JOURNAL_SHEET_URL",      "JOURNAL_SHEET_TAB",      "Sheet1"),
    "bankreceipt":     ("tally-bankreceipt-sync-outstanding",      "fetch_tally_bankreceipt.py",  "BANK_RECEIPT_SHEET_URL", "BANK_RECEIPT_SHEET_TAB", "Sheet1"),
    "chequereturn":    ("tally-chequereturn-sync-outstanding",     "fetch_tally_chqreturn.py",    "CHQ_RETURN_SHEET_URL",   "CHQ_RETURN_SHEET_TAB",   "Sheet1"),
}

SHEET_ID_RE = re.compile(r"/spreadsheets/d/([a-zA-Z0-9-_]+)")
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]


# ---------------------------------------------------------------------------
# Google Sheets helpers
# ---------------------------------------------------------------------------

def _authorize() -> Any:
    load_dotenv(PROJECT_ROOT / ".env")
    creds_path = os.environ["GOOGLE_CREDENTIALS_FILE"]
    token_path  = os.environ["GOOGLE_TOKEN_FILE"]
    creds: Credentials | None = None
    if Path(token_path).exists():
        creds = Credentials.from_authorized_user_file(token_path, SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            raise RuntimeError(
                "Google token missing or expired. Open the dashboard and run any sync "
                "once to re-authorise, then retry the audit."
            )
    return build("sheets", "v4", credentials=creds, cache_discovery=False)


def _read_sheet(svc: Any, sheet_id: str, tab: str) -> list[list[str]]:
    resp = svc.spreadsheets().values().get(
        spreadsheetId=sheet_id,
        range=f"'{tab}'",
        majorDimension="ROWS",
    ).execute()
    return resp.get("values", [])


def _col(headers: list[str], *candidates: str) -> int | None:
    lower = [h.strip().lower() for h in headers]
    for c in candidates:
        try:
            return lower.index(c.strip().lower())
        except ValueError:
            pass
    return None


def _parse_sheet_date(s: str) -> date | None:
    for fmt in ("%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y"):
        try:
            return datetime.strptime(s.strip(), fmt).date()
        except ValueError:
            pass
    return None


def _extract_rows(data: Any) -> list[dict]:
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        return data.get("vouchers", [])
    return []


# ---------------------------------------------------------------------------
# Date chunking
# ---------------------------------------------------------------------------

def _fmt_dmy(d: date) -> str:
    return d.strftime("%d-%m-%Y")


def _month_chunks(start: date, end: date) -> list[tuple[date, date]]:
    chunks: list[tuple[date, date]] = []
    cur = start
    while cur <= end:
        if cur.month == 12:
            month_end = date(cur.year, 12, 31)
        else:
            month_end = date(cur.year, cur.month + 1, 1) - timedelta(days=1)
        chunks.append((cur, min(month_end, end)))
        cur = month_end + timedelta(days=1)
    return chunks


# ---------------------------------------------------------------------------
# AuditRunner
# ---------------------------------------------------------------------------

class AuditRunner:
    """
    Runs the cancellation audit in a background thread.

    Events emitted via drain_events():
        {"kind": "progress",     "master": str, "company": str, "chunk": str, "step": int, "total": int}
        {"kind": "reading_sheet","master": str}
        {"kind": "master_done",  "master": str, "sheet_rows": int, "tally_rows": int, "flagged": int}
        {"kind": "warn",         "master": str, "msg": str}
        {"kind": "error",        "master": str, "msg": str}
        {"kind": "all_done",     "count": int}
    """

    def __init__(
        self,
        companies: list[str],
        masters: list[str],
        from_date: date,
        to_date: date,
    ) -> None:
        self.companies  = companies
        self.masters    = [m for m in masters if m in MASTER_CONFIG]
        self.from_date  = from_date
        self.to_date    = to_date
        self.results:   list[dict] = []
        self.error:     str | None = None
        self._events:   deque[dict] = deque()
        self._lock      = threading.Lock()
        self._done      = threading.Event()
        self._thread:   threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def is_running(self) -> bool:
        return not self._done.is_set()

    def drain_events(self) -> list[dict]:
        with self._lock:
            out = list(self._events)
            self._events.clear()
        return out

    def _emit(self, ev: dict) -> None:
        with self._lock:
            self._events.append(ev)

    # -- main worker ---------------------------------------------------------

    def _run(self) -> None:
        try:
            load_dotenv(PROJECT_ROOT / ".env")
            svc = _authorize()

            chunks = _month_chunks(self.from_date, self.to_date)
            total_steps = len(self.masters) * len(self.companies) * len(chunks)
            step = 0

            # Step 1: fetch active vouchers from Tally per master × company × month
            # tally_keys[master] = set of (company, location, voucher_no) still active in Tally
            tally_keys: dict[str, set[tuple[str, str, str]]] = {m: set() for m in self.masters}

            for master in self.masters:
                skill_dir, fetch_script = MASTER_CONFIG[master][0], MASTER_CONFIG[master][1]
                script = str(SKILLS_ROOT / skill_dir / "tools" / fetch_script)

                for company in self.companies:
                    for chunk_start, chunk_end in chunks:
                        step += 1
                        self._emit({
                            "kind":    "progress",
                            "master":  master,
                            "company": company,
                            "chunk":   chunk_start.strftime("%b %Y"),
                            "step":    step,
                            "total":   total_steps,
                        })

                        tmp = TMP_ROOT / f"audit_{master}_{int(time.time()*1000)}.json"
                        TMP_ROOT.mkdir(parents=True, exist_ok=True)
                        cmd = [
                            sys.executable or "python", script,
                            "--from", _fmt_dmy(chunk_start),
                            "--to",   _fmt_dmy(chunk_end),
                            "--companies", company,
                            "--output", str(tmp),
                        ]
                        proc = subprocess.run(cmd, capture_output=True, text=True, cwd=str(PROJECT_ROOT))
                        if proc.returncode == 0 and tmp.exists():
                            try:
                                data = json.loads(tmp.read_text(encoding="utf-8"))
                                for r in _extract_rows(data):
                                    key = (
                                        str(r.get("company", "")).strip(),
                                        str(r.get("location", "")).strip(),
                                        str(r.get("voucher_no", "")).strip(),
                                    )
                                    if any(key):
                                        tally_keys[master].add(key)
                            except Exception:
                                pass
                            tmp.unlink(missing_ok=True)

            # Step 2: read each sheet, compare against Tally keys
            flagged: list[dict] = []

            for master in self.masters:
                _, _, url_env, tab_env, default_tab = MASTER_CONFIG[master]
                url = os.environ.get(url_env, "").strip()
                tab = os.environ.get(tab_env, default_tab)

                if not url:
                    self._emit({"kind": "warn", "master": master, "msg": f"{url_env} not set — skipped"})
                    continue

                self._emit({"kind": "reading_sheet", "master": master})

                try:
                    m = SHEET_ID_RE.search(url)
                    if not m:
                        raise ValueError(f"Cannot parse sheet ID from {url_env}")
                    sheet_id = m.group(1)
                    rows = _read_sheet(svc, sheet_id, tab)
                except Exception as exc:
                    self._emit({"kind": "error", "master": master, "msg": f"Sheet read failed: {exc}"})
                    continue

                if len(rows) < 2:
                    self._emit({"kind": "master_done", "master": master,
                                "sheet_rows": 0, "tally_rows": len(tally_keys[master]), "flagged": 0})
                    continue

                headers = rows[0]
                co_idx   = _col(headers, "Company")
                loc_idx  = _col(headers, "Location")
                vno_idx  = _col(headers, "Voucher No.", "Voucher No", "VoucherNo", "Vch No.", "Vch No")
                date_idx = _col(headers, "Date", "Receipt Date", "Voucher Date")
                par_idx  = _col(headers, "Particulars", "Customer Name", "Party Name", "Ledger Name")
                amt_idx  = _col(headers, "Amount", "Value", "Gross Total", "Receipt Amt", "Debit", "Credit")

                if any(i is None for i in (co_idx, loc_idx, vno_idx)):
                    self._emit({"kind": "warn", "master": master,
                                "msg": "Sheet missing Company/Location/Voucher No. columns — skipped"})
                    continue

                def cell(row: list[str], idx: int | None) -> str:
                    return row[idx].strip() if idx is not None and idx < len(row) else ""

                # Collect unique sheet vouchers within the audit date range
                seen: dict[tuple[str, str, str], dict] = {}
                for row in rows[1:]:
                    date_str = cell(row, date_idx)
                    d = _parse_sheet_date(date_str) if date_str else None
                    # Skip rows outside the audit window (avoids false positives)
                    if d and not (self.from_date <= d <= self.to_date):
                        continue
                    key = (cell(row, co_idx), cell(row, loc_idx), cell(row, vno_idx))
                    if not any(key):
                        continue
                    if key not in seen:
                        seen[key] = {
                            "master":      master,
                            "company":     key[0],
                            "location":    key[1],
                            "voucher_no":  key[2],
                            "date":        date_str,
                            "particulars": cell(row, par_idx),
                            "amount":      cell(row, amt_idx),
                            "remark":      "Absent from Tally — verify if deleted or cancelled",
                        }

                sheet_key_count = len(seen)
                tally_key_count = len(tally_keys[master])
                master_flagged  = 0
                for key, info in seen.items():
                    if key not in tally_keys[master]:
                        flagged.append(info)
                        master_flagged += 1

                self._emit({
                    "kind":       "master_done",
                    "master":     master,
                    "sheet_rows": sheet_key_count,
                    "tally_rows": tally_key_count,
                    "flagged":    master_flagged,
                })

            self.results = flagged

        except Exception as exc:
            self.error = str(exc)
            self._emit({"kind": "error", "master": "—", "msg": str(exc)})
        finally:
            self._done.set()
            self._emit({"kind": "all_done", "count": len(self.results)})
