"""Fetch each Sundry-Debtor ledger's CURRENT bill-wise outstanding (BILLCL, with
due dates) from Tally Prime via the per-ledger "Ledger Outstandings" report.

Why per-ledger: whole-company bill/outstanding requests HANG on this Tally build
(and can crash the gateway). The per-ledger "Ledger Outstandings" report returns
in ~0.2s with each open bill's date, ref, balance and DUE DATE — including
future-dated installments. See OVERDUE_RECONCILE_PLAN §2C-WORKING.

Pipeline:
  1. List Sundry-Debtor ledgers (Group + Ledger collections; parent-chain walk).
  2. For each, fetch open bills as-on the snapshot date (BILLCL).
  3. Emit one JSON row per open bill (schema = reference/columns.md).

process_data.py consumes the pushed sheet via TALLY_BILLWISE_SHEET_URL/_TAB and,
for any party whose calculated outstanding disagrees with Tally, sources that
party's outstanding + overdue + aging from these bills.

Sequential, one company at a time, paced (--delay) — the crash guardrail.

Usage (run from this tools/ dir so .env + _schema resolve):
  python fetch_tally_billwise.py --companies "ORANGE O TEC PRIVATE LIMITED (01-04-25TO31-03-27)" \
      --output .tmp/dashboard_billwise_<co>_<ts>.json [--as-of 2026-05-28] [--delay 0.2]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime, date, timezone
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _schema import load_columns, load_companies  # noqa: E402
from list_tally_companies import list_companies  # noqa: E402

_INVALID = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f�]")
_NUMREF = re.compile(r"&#(x[0-9a-fA-F]+|\d+);")
_BARE_AMP = re.compile(r"&(?!(?:amp|lt|gt|quot|apos|#\d+|#x[0-9a-fA-F]+);)")


def _esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _san(c: bytes) -> bytes:
    text = _INVALID.sub("", c.decode("utf-8", errors="ignore"))

    def _ref(m):
        ref = m.group(1)
        try:
            n = int(ref[1:], 16) if ref.startswith("x") else int(ref)
        except ValueError:
            return ""
        if n in (0x09, 0x0A, 0x0D) or 0x20 <= n <= 0xD7FF or 0xE000 <= n <= 0xFFFD or 0x10000 <= n <= 0x10FFFF:
            return m.group(0)
        return ""

    text = _NUMREF.sub(_ref, text)
    text = _BARE_AMP.sub("&amp;", text)
    return text.encode("utf-8")


def _t(el: ET.Element | None) -> str:
    return "" if el is None or el.text is None else el.text.strip()


def _to_iso(s: str) -> str:
    s = (s or "").strip()
    if not s:
        return ""
    for fmt in ("%d-%b-%y", "%d-%b-%Y", "%d-%m-%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            continue
    return ""


def _fnum(s: str) -> float:
    try:
        return float((s or "").replace(",", "").strip())
    except ValueError:
        return 0.0


def fetch_debtor_ledgers(host: str, company: str) -> list[tuple[str, str]]:
    """Return [(ledger_name, guid), ...] for ledgers whose parent chain hits
    'Sundry Debtors'. Uses Group + Ledger collections (both return fast)."""
    # 1) groups → parent map
    gbody = (
        f"<ENVELOPE><HEADER><VERSION>1</VERSION><TALLYREQUEST>Export</TALLYREQUEST>"
        f"<TYPE>Collection</TYPE><ID>AllGroups</ID></HEADER><BODY><DESC><STATICVARIABLES>"
        f"<SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>"
        f"<SVCURRENTCOMPANY>{_esc(company)}</SVCURRENTCOMPANY></STATICVARIABLES><TDL><TDLMESSAGE>"
        f'<COLLECTION NAME="AllGroups" ISMODIFY="No"><TYPE>Group</TYPE><FETCH>NAME,PARENT</FETCH></COLLECTION>'
        f"</TDLMESSAGE></TDL></DESC></BODY></ENVELOPE>"
    )
    r = requests.post(host, data=gbody.encode("utf-8"), timeout=60)
    r.raise_for_status()
    root = ET.fromstring(_san(r.content))
    parent_map: dict[str, str] = {}
    for g in root.iter("GROUP"):
        nm = (g.attrib.get("NAME") or _t(g.find("NAME"))).strip()
        if nm:
            parent_map[nm] = _t(g.find("PARENT"))

    def hits_debtors(parent: str) -> bool:
        cur, seen = parent, set()
        while cur and cur not in seen:
            if cur == "Sundry Debtors":
                return True
            seen.add(cur)
            cur = parent_map.get(cur, "")
        return False

    # 2) ledgers → keep those whose parent chain hits Sundry Debtors
    lbody = (
        f"<ENVELOPE><HEADER><VERSION>1</VERSION><TALLYREQUEST>Export</TALLYREQUEST>"
        f"<TYPE>Collection</TYPE><ID>AllLedgers</ID></HEADER><BODY><DESC><STATICVARIABLES>"
        f"<SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>"
        f"<SVCURRENTCOMPANY>{_esc(company)}</SVCURRENTCOMPANY></STATICVARIABLES><TDL><TDLMESSAGE>"
        f'<COLLECTION NAME="AllLedgers" ISMODIFY="No"><TYPE>Ledger</TYPE><FETCH>NAME,GUID,PARENT</FETCH></COLLECTION>'
        f"</TDLMESSAGE></TDL></DESC></BODY></ENVELOPE>"
    )
    r = requests.post(host, data=lbody.encode("utf-8"), timeout=240)
    r.raise_for_status()
    root = ET.fromstring(_san(r.content))
    out: list[tuple[str, str]] = []
    for led in root.iter("LEDGER"):
        nm = (led.attrib.get("NAME") or _t(led.find("NAME"))).strip()
        if nm and hits_debtors(_t(led.find("PARENT"))):
            out.append((nm, _t(led.find("GUID"))))
    return out


def fetch_ledger_bills(host: str, company: str, ledger: str, as_of_yyyymmdd: str, timeout: int) -> list[dict]:
    """Open bills (BILLCL) for one ledger via Ledger Outstandings. Flat positional
    stream: <BILLFIXED>(BILLDATE,BILLREF) then sibling BILLOP/BILLCL/BILLDUE."""
    body = (
        f"<ENVELOPE><HEADER><VERSION>1</VERSION><TALLYREQUEST>Export</TALLYREQUEST>"
        f"<TYPE>Data</TYPE><ID>Ledger Outstandings</ID></HEADER><BODY><DESC><STATICVARIABLES>"
        f"<SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>"
        f"<SVCURRENTCOMPANY>{_esc(company)}</SVCURRENTCOMPANY>"
        f'<SVFROMDATE TYPE="Date">{as_of_yyyymmdd}</SVFROMDATE>'
        f'<SVTODATE TYPE="Date">{as_of_yyyymmdd}</SVTODATE>'
        f"<LEDGERNAME>{_esc(ledger)}</LEDGERNAME></STATICVARIABLES></DESC></BODY></ENVELOPE>"
    )
    r = requests.post(host, data=body.encode("utf-8"), timeout=timeout)
    r.raise_for_status()
    root = ET.fromstring(_san(r.content))
    bills: list[dict] = []
    cur: dict | None = None
    for el in list(root):
        tag = el.tag.upper()
        if tag == "BILLFIXED":
            cur = {"bill_ref": (el.findtext("BILLREF") or "").strip(),
                   "bill_date": _to_iso(el.findtext("BILLDATE") or ""),
                   "due_date": "", "amount": 0.0}
            bills.append(cur)
        elif cur is not None and tag == "BILLCL":
            cur["amount"] = _fnum(el.text or "")
        elif cur is not None and tag == "BILLDUE":
            cur["due_date"] = _to_iso(el.text or "")
    return [b for b in bills if b["bill_ref"] and abs(b["amount"]) > 0.01]


def main() -> int:
    ap = argparse.ArgumentParser(description="Fetch per-ledger bill-wise outstanding (BILLCL) from Tally.")
    ap.add_argument("--companies", default="", help="Comma-separated raw Tally company names. Blank = all loaded.")
    ap.add_argument("--output", required=True, help="Path to write the JSON rows.")
    ap.add_argument("--as-of", default="", help="Snapshot date YYYY-MM-DD (default: today).")
    ap.add_argument("--delay", type=float, default=0.2, help="Seconds between per-ledger requests (crash guard).")
    ap.add_argument("--timeout", type=int, default=30)
    args = ap.parse_args()

    load_dotenv()
    host = os.environ.get("TALLY_HOST", "http://localhost:9000").rstrip("/")
    as_of_d = datetime.strptime(args.as_of, "%Y-%m-%d").date() if args.as_of else date.today()
    as_of_iso = as_of_d.isoformat()
    as_of_yyyymmdd = as_of_d.strftime("%Y%m%d")

    company_map = load_companies()
    columns = load_columns()  # validates schema parses
    _ = columns

    if args.companies.strip():
        raw_companies = [c.strip() for c in args.companies.split(",") if c.strip()]
    else:
        raw_companies = list_companies(host)

    rows: list[dict[str, Any]] = []
    summary_companies: list[dict] = []
    for raw in raw_companies:
        display, location = company_map.get(raw, (raw, ""))
        if raw not in company_map:
            print(f"WARNING: '{raw}' not in companies.md — using raw name, blank location.", file=sys.stderr)
        print(f"[billwise] {raw} → {display}/{location}; listing debtors…", flush=True)
        debtors = fetch_debtor_ledgers(host, raw)
        print(f"[billwise] {len(debtors)} Sundry-Debtor ledgers; fetching bills (as-on {as_of_iso})…", flush=True)
        n_bills = n_err = 0
        for i, (name, guid) in enumerate(debtors, 1):
            try:
                bills = fetch_ledger_bills(host, raw, name, as_of_yyyymmdd, args.timeout)
            except Exception as exc:
                n_err += 1
                print(f"  [{i}/{len(debtors)}] ERR {name[:40]}: {type(exc).__name__}", flush=True)
                time.sleep(args.delay)
                continue
            for b in bills:
                amt = b["amount"]
                rows.append({
                    "company": display, "location": location,
                    "ledger_id": guid, "ledger_name": name,
                    "bill_ref_name": b["bill_ref"], "bill_date": b["bill_date"],
                    "due_date": b["due_date"], "closing_balance": f"{abs(amt):.2f}",
                    "dr_cr": "Dr" if amt < 0 else "Cr", "as_of_date": as_of_iso,
                })
                n_bills += 1
            if i % 50 == 0:
                print(f"  …{i}/{len(debtors)} ({n_bills} bills)", flush=True)
            time.sleep(args.delay)
        summary_companies.append({"company": display, "location": location,
                                  "debtors": len(debtors), "bills": n_bills, "errors": n_err})
        print(f"[billwise] {display}/{location}: {len(debtors)} debtors, {n_bills} bills, {n_err} errors", flush=True)

    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    Path(args.output).write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    summary = {"rows": len(rows), "as_of": as_of_iso, "companies": summary_companies, "output": args.output}
    print(json.dumps(summary))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
