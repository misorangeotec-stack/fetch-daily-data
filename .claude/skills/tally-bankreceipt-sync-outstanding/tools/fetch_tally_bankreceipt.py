"""Fetch receipt vouchers (Bank Receipt, Cash Receipt, JV Receipt, etc.) from
Tally Prime via XML over HTTP.

Loops over each loaded company (or a user-supplied subset), pulls all
voucher types whose PARENT class is ``Receipt`` for the date range, and
emits **one row per bill allocation** (matches the reference Bank
Receipt.xlsx granularity). Vouchers without a bill allocation emit a single
``On Account`` row using the voucher-level party amount.

Company / location values are taken from ``reference/companies.md`` so the
sheet shows clean display names instead of Tally's raw fiscal-year-stamped
names. Unmapped companies emit a one-time stderr warning and fall back to
the raw name with blank location.

Required env vars (loaded from project .env):
    TALLY_HOST   e.g. http://localhost:9000

Usage:
    python fetch_tally_bankreceipt.py --from 01-04-2026 --to 25-04-2026 \\
        [--companies "Co1,Co2"] --output .tmp/bankreceipt_<ts>.json
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

import requests
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _schema import Column, load_columns, load_companies  # noqa: E402

from list_tally_companies import list_companies  # noqa: E402


# Parent classes that mark a voucher type as a receipt transaction. Tally
# groups bank-receipt, cash-receipt, journal-receipt etc. all under PARENT
# 'Receipt'. Some companies use the alternative 'Receipts' (with the s) —
# accept both. If a new variant appears in the wild, add it here.
RECEIPT_PARENT_CLASSES: set[str] = {"receipt", "receipts"}


def build_voucher_register_xml(company: str, from_date: str, to_date: str, vch_type: str) -> str:
    """Voucher Register query restricted to a single voucher type for the date range.

    Same approach as the sales sync: enumerate every voucher type whose
    PARENT is in ``RECEIPT_PARENT_CLASSES`` and run one Voucher Register
    query per type. Voucher Register reliably honours ``VOUCHERTYPENAME`` as
    a filter; the alternative ``Day Book`` / ``Receipt Vouchers`` reports
    drop categories silently.
    """
    return f"""\
<ENVELOPE>
  <HEADER>
    <TALLYREQUEST>Export Data</TALLYREQUEST>
  </HEADER>
  <BODY>
    <EXPORTDATA>
      <REQUESTDESC>
        <REPORTNAME>Voucher Register</REPORTNAME>
        <STATICVARIABLES>
          <SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
          <SVCURRENTCOMPANY>{_escape(company)}</SVCURRENTCOMPANY>
          <SVFROMDATE TYPE="Date">{from_date}</SVFROMDATE>
          <SVTODATE TYPE="Date">{to_date}</SVTODATE>
          <VOUCHERTYPENAME>{_escape(vch_type)}</VOUCHERTYPENAME>
        </STATICVARIABLES>
      </REQUESTDESC>
    </EXPORTDATA>
  </BODY>
</ENVELOPE>
"""


def list_receipt_voucher_types(host: str, company: str) -> list[str]:
    """Return names of all voucher types whose PARENT is a receipt class for
    the given company (see ``RECEIPT_PARENT_CLASSES``).
    """
    body = f"""<ENVELOPE>
<HEADER><VERSION>1</VERSION><TALLYREQUEST>Export</TALLYREQUEST><TYPE>Collection</TYPE><ID>VchTypes</ID></HEADER>
<BODY><DESC>
<STATICVARIABLES>
<SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
<SVCURRENTCOMPANY>{_escape(company)}</SVCURRENTCOMPANY>
</STATICVARIABLES>
<TDL><TDLMESSAGE>
<COLLECTION NAME="VchTypes" ISMODIFY="No"><TYPE>VoucherType</TYPE><FETCH>NAME,PARENT</FETCH></COLLECTION>
</TDLMESSAGE></TDL>
</DESC></BODY></ENVELOPE>"""
    resp = requests.post(host, data=body.encode("utf-8"), timeout=60)
    if resp.status_code != 200:
        raise SystemExit(f"ERROR listing voucher types for '{company}': HTTP {resp.status_code}")
    cleaned = _sanitize_tally_xml(resp.content)
    root = ET.fromstring(cleaned)
    out: list[str] = []
    for vt in root.iter("VOUCHERTYPE"):
        name = (vt.attrib.get("NAME") or _text(vt.find("NAME"))).strip()
        parent = _text(vt.find("PARENT"))
        if name and parent.lower() in RECEIPT_PARENT_CLASSES:
            out.append(name)
    seen: set[str] = set()
    return [n for n in out if not (n in seen or seen.add(n))]


def _escape(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def parse_dmy(s: str) -> str:
    """Convert DD-MM-YYYY → YYYYMMDD (Tally's wire format)."""
    return datetime.strptime(s, "%d-%m-%Y").strftime("%Y%m%d")


_INVALID_XML_CHARS_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f�]")
_NUMERIC_CHAR_REF_RE = re.compile(r"&#(x[0-9a-fA-F]+|\d+);")


def _sanitize_tally_xml(content: bytes) -> bytes:
    """Tally XML mixes invalid UTF-8 bytes, raw control chars, and numeric
    char refs that XML 1.0 forbids. Decode tolerantly, strip, re-encode.
    """
    text = content.decode("utf-8", errors="ignore")
    text = _INVALID_XML_CHARS_RE.sub("", text)

    def _replace(m: "re.Match[str]") -> str:
        ref = m.group(1)
        try:
            n = int(ref[1:], 16) if ref.startswith("x") else int(ref)
        except ValueError:
            return ""
        if n in (0x09, 0x0A, 0x0D) or 0x20 <= n <= 0xD7FF or 0xE000 <= n <= 0xFFFD or 0x10000 <= n <= 0x10FFFF:
            return m.group(0)
        return ""

    text = _NUMERIC_CHAR_REF_RE.sub(_replace, text)
    return text.encode("utf-8")


def fetch_ledger_guid_map(host: str, company: str) -> dict[str, str]:
    """Return ``{UPPER(TRIM(ledger_name)): GUID}`` for every ledger in a company.

    Used to stamp a stable Tally GUID (``ledger_id``) onto each receipt row so
    customer identity survives ledger renames (the name text can change; the
    GUID never does). See ``scripts/LEDGER_ID_MIGRATION.md`` in the receivables
    repo. Reuses the same Ledger collection query as
    ``tally-ledger-master-sync/fetch_tally_ledger_master.py``.

    **Best effort:** on ANY failure this returns ``{}`` and the caller falls
    back to a blank ``ledger_id``. It must never break the daily fetch — the
    GUID is an additive enrichment, not a hard dependency.
    """
    body = f"""<ENVELOPE>
<HEADER><VERSION>1</VERSION><TALLYREQUEST>Export</TALLYREQUEST><TYPE>Collection</TYPE><ID>GuidMap</ID></HEADER>
<BODY><DESC>
<STATICVARIABLES>
<SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
<SVCURRENTCOMPANY>{_escape(company)}</SVCURRENTCOMPANY>
</STATICVARIABLES>
<TDL><TDLMESSAGE>
<COLLECTION NAME="GuidMap" ISMODIFY="No"><TYPE>Ledger</TYPE><FETCH>NAME,GUID</FETCH></COLLECTION>
</TDLMESSAGE></TDL>
</DESC></BODY></ENVELOPE>"""
    try:
        resp = requests.post(host, data=body.encode("utf-8"), timeout=120)
        if resp.status_code != 200:
            print(
                f"WARNING: ledger GUID map fetch for '{company}' returned HTTP "
                f"{resp.status_code} — ledger_id left blank for this company.",
                file=sys.stderr,
            )
            return {}
        root = ET.fromstring(_sanitize_tally_xml(resp.content))
    except Exception as exc:  # noqa: BLE001 — never break the fetch over the GUID map
        print(
            f"WARNING: ledger GUID map fetch for '{company}' failed ({exc}) — "
            "ledger_id left blank for this company.",
            file=sys.stderr,
        )
        return {}
    out: dict[str, str] = {}
    for led in root.iter("LEDGER"):
        name = (led.attrib.get("NAME") or _text(led.find("NAME"))).strip()
        guid = _text(led.find("GUID"))
        if name and guid:
            out[name.upper()] = guid  # Tally enforces unique ledger names per company
    return out


def fetch_company_vouchers(host: str, company: str, from_date: str, to_date: str) -> list[ET.Element]:
    """Fetch all receipt-category vouchers for one company in the date range."""
    receipt_types = list_receipt_voucher_types(host, company)
    all_vouchers: list[ET.Element] = []
    for vch_type in receipt_types:
        body = build_voucher_register_xml(company, from_date, to_date, vch_type).encode("utf-8")
        resp = requests.post(host, data=body, timeout=180)
        if resp.status_code != 200:
            raise SystemExit(
                f"ERROR fetching vouchers for '{company}' / '{vch_type}': "
                f"HTTP {resp.status_code}\n{resp.text[:400]}"
            )
        cleaned = _sanitize_tally_xml(resp.content)
        try:
            root = ET.fromstring(cleaned)
        except ET.ParseError as exc:
            raise SystemExit(
                f"ERROR parsing voucher XML for '{company}' / '{vch_type}': {exc}\n"
                f"{cleaned[:500].decode('utf-8', errors='replace')}"
            )
        for v in root.iter("VOUCHER"):
            if not _is_cancelled(v):
                all_vouchers.append(v)
    return all_vouchers


def _is_cancelled(voucher: ET.Element) -> bool:
    if voucher.attrib.get("ACTION", "").lower() == "cancel":
        return True
    if _text(voucher.find("ISCANCELLED")).lower() in {"yes", "true", "1"}:
        return True
    return _text(voucher.find("ISOPTIONAL")).lower() in {"yes", "true", "1"}


def _text(el: ET.Element | None) -> str:
    if el is None or el.text is None:
        return ""
    return el.text.strip()


def derive_month(date_yyyymmdd: str) -> str:
    if not date_yyyymmdd or len(date_yyyymmdd) < 6:
        return ""
    try:
        return datetime.strptime(date_yyyymmdd, "%Y%m%d").strftime("%Y-%m")
    except ValueError:
        return ""


def format_date(date_yyyymmdd: str) -> str:
    if not date_yyyymmdd or len(date_yyyymmdd) < 8:
        return ""
    try:
        return datetime.strptime(date_yyyymmdd, "%Y%m%d").strftime("%d/%m/%Y")
    except ValueError:
        return date_yyyymmdd


def _amount_abs(amount_str: str) -> str:
    """Tally amounts are signed strings (e.g. '-97232.00'). The reference
    Excel always shows the receipt amount as a positive number, regardless
    of which side of the books it sits on. Strip the sign; preserve the
    original formatting otherwise.
    """
    if not amount_str:
        return ""
    s = amount_str.strip()
    if s.startswith(("-", "+")):
        s = s[1:]
    return s.strip()


def _trans_type_from_amount(amount_str: str) -> str:
    """Derive Credit/Debit from the sign of a Tally amount string.
    In a receipt voucher, Tally stores bill allocation amounts as positive
    for the normal credit-side (party being paid = Credit) and negative
    for debit adjustments within the same voucher.
    """
    s = amount_str.strip() if amount_str else ""
    if not s:
        return ""
    return "Debit" if s.startswith("-") else "Credit"


def _voucher_id(voucher: ET.Element) -> str:
    """Best-effort unique identifier for a voucher.

    For bank/cash receipts, Tally often stores no VOUCHERNUMBER (the bank
    statement reference is the de-facto identifier), so we fall back to GUID
    and finally to the REMOTEID attribute. One of these is always present;
    GUID is globally unique.
    """
    for tag in ("VOUCHERNUMBER", "GUID"):
        v = _text(voucher.find(tag))
        if v:
            return v
    return voucher.attrib.get("REMOTEID", "").strip()


def _is_real_allocation(alloc: ET.Element) -> bool:
    """Tally emits an empty ``<BILLALLOCATIONS.LIST/>`` (zero children) on
    On-Account vouchers instead of omitting the element. Treat those as
    "no allocation" so the On-Account branch handles them.
    """
    return bool(_text(alloc.find("NAME")) or _text(alloc.find("BILLTYPE")) or _text(alloc.find("AMOUNT")))


def _voucher_to_rows(
    voucher: ET.Element,
    company: str,
    location: str,
    guid_map: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Emit one row per bill allocation, per party.

    A single Receipt voucher in Tally can credit multiple debtor parties at
    once (e.g. one bank receipt settling invoices for "THE FESTON(MACHINE)"
    and "THE FESTON" in the same entry). The header's PARTYLEDGERNAME only
    names one of them, so we iterate every party-side ledger entry.

    Party-side entries are identified by ISDEEMEDPOSITIVE != Yes (in a
    receipt the bank/cash is Dr, parties are Cr). This rule works uniformly
    for Bank Receipt, Cash Receipt and JV Receipt without a ledger-master
    lookup.
    """
    date_raw = _text(voucher.find("DATE"))
    voucher_type_name = _text(voucher.find("VOUCHERTYPENAME"))
    voucher_no = _voucher_id(voucher)
    header_party = _text(voucher.find("PARTYLEDGERNAME")) or _text(voucher.find("PARTYNAME"))

    base: dict[str, Any] = {
        "company": company,
        "location": location,
        "month": derive_month(date_raw),
        "voucher_type": voucher_type_name,
        "voucher_no": voucher_no,
        "receipt_date": format_date(date_raw),
    }

    all_entries = list(voucher.findall("LEDGERENTRIES.LIST")) + list(voucher.findall("ALLLEDGERENTRIES.LIST"))
    party_entries = [
        e for e in all_entries
        if _text(e.find("ISDEEMEDPOSITIVE")).lower() not in {"yes", "true", "1"}
    ]

    guid_map = guid_map or {}

    def _stamp(rows_: list[dict[str, Any]]) -> list[dict[str, Any]]:
        # Resolve customer_name → GUID (ledger_id). Display-only name stays on
        # the row; identity will key on this GUID (LEDGER_ID_MIGRATION.md).
        for r in rows_:
            r["ledger_id"] = guid_map.get((r.get("customer_name") or "").strip().upper(), "")
        return rows_

    rows: list[dict[str, Any]] = []

    if not party_entries:
        # Defensive fallback: malformed voucher with no Cr-side entry. Don't
        # silently drop it — emit a single On Account row using the header
        # party name and whatever amount we can find.
        row = dict(base)
        row["customer_name"] = header_party
        row["ref_inv_no"] = ""
        row["receipt_amt"] = ""
        row["trans_type"] = ""
        row["allocation_type"] = "On Account"
        rows.append(row)
        return _stamp(rows)

    for entry in party_entries:
        customer_name = _text(entry.find("LEDGERNAME"))
        allocations = [a for a in entry.findall("BILLALLOCATIONS.LIST") if _is_real_allocation(a)]

        if allocations:
            for alloc in allocations:
                ref_name = _text(alloc.find("NAME"))
                bill_type = _text(alloc.find("BILLTYPE"))
                amt = _text(alloc.find("AMOUNT"))
                row = dict(base)
                row["customer_name"] = customer_name
                row["ref_inv_no"] = ref_name
                row["receipt_amt"] = _amount_abs(amt)
                row["trans_type"] = _trans_type_from_amount(amt)
                row["allocation_type"] = bill_type or "On Account"
                rows.append(row)
        else:
            amt = _text(entry.find("AMOUNT"))
            row = dict(base)
            row["customer_name"] = customer_name
            row["ref_inv_no"] = ""
            row["receipt_amt"] = _amount_abs(amt)
            row["trans_type"] = _trans_type_from_amount(amt)
            row["allocation_type"] = "On Account"
            rows.append(row)

    return _stamp(rows)


def project_columns(rows: Iterable[dict[str, Any]], columns: list[Column]) -> list[dict[str, Any]]:
    keys = [c.key for c in columns]
    out: list[dict[str, Any]] = []
    for r in rows:
        d = {k: r.get(k, "") for k in keys}
        # Carry the GUID as an extra field even though it is not (yet) a
        # column in reference/columns.md. The push tool only writes columns.md
        # columns, so this is invisible to the sheet / daily sync until
        # ledger_id is promoted to a real column at the migration gate.
        if "ledger_id" in r:
            d["ledger_id"] = r.get("ledger_id", "")
        out.append(d)
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="Fetch Tally receipt vouchers as JSON.")
    parser.add_argument("--from", dest="from_date", required=True, help="Start date DD-MM-YYYY")
    parser.add_argument("--to", dest="to_date", required=True, help="End date DD-MM-YYYY")
    parser.add_argument("--companies", default="", help="Comma-separated subset of loaded companies (default: all)")
    parser.add_argument("--output", required=True, help="Output JSON path (e.g. .tmp/bankreceipt_<ts>.json)")
    args = parser.parse_args()

    load_dotenv()
    host = os.environ.get("TALLY_HOST", "http://localhost:9000").rstrip("/")

    from_date = parse_dmy(args.from_date)
    to_date = parse_dmy(args.to_date)

    columns = load_columns()
    company_map = load_companies()
    loaded = list_companies(host)
    if not loaded:
        raise SystemExit("ERROR: no companies are loaded in Tally. Load at least one company and retry.")

    if args.companies.strip():
        wanted = {c.strip() for c in args.companies.split(",") if c.strip()}
        unknown = wanted - set(loaded)
        if unknown:
            raise SystemExit(f"ERROR: requested companies not loaded in Tally: {sorted(unknown)}. Loaded: {loaded}")
        target_companies = [c for c in loaded if c in wanted]
    else:
        target_companies = loaded

    warned: set[str] = set()
    all_rows: list[dict[str, Any]] = []
    voucher_count = 0
    for raw_company in target_companies:
        if raw_company in company_map:
            display_company, location = company_map[raw_company]
        else:
            if raw_company not in warned:
                print(
                    f"WARNING: '{raw_company}' is not in reference/companies.md — "
                    "using raw name and blank location. Add a row to the mapping "
                    "to fix this.",
                    file=sys.stderr,
                )
                warned.add(raw_company)
            display_company, location = raw_company, ""

        # Build the per-company name→GUID map once (best effort; blank GUIDs
        # if it fails). Resolution is per-company because the same party name
        # can exist in multiple companies with different GUIDs.
        guid_map = fetch_ledger_guid_map(host, raw_company)

        vouchers = fetch_company_vouchers(host, raw_company, from_date, to_date)
        voucher_count += len(vouchers)
        for v in vouchers:
            all_rows.extend(
                _voucher_to_rows(v, company=display_company, location=location, guid_map=guid_map)
            )

    rows = project_columns(all_rows, columns)

    # ledger_id resolution stats (D5 visibility): how many party rows resolved
    # to a Tally GUID vs not (a blank GUID = mis-booking or missing master).
    named = [r for r in all_rows if (r.get("customer_name") or "").strip()]
    resolved = sum(1 for r in named if (r.get("ledger_id") or "").strip())
    unresolved = len(named) - resolved
    unresolved_names = sorted({
        r["customer_name"].strip()
        for r in named
        if not (r.get("ledger_id") or "").strip()
    })

    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(rows, indent=2, ensure_ascii=False), encoding="utf-8")

    summary = {
        "vouchers": voucher_count,
        "rows": len(rows),
        "companies": [company_map.get(c, (c, ""))[0] for c in target_companies],
        "ledger_id_resolved": resolved,
        "ledger_id_unresolved": unresolved,
        "ledger_id_unresolved_names": unresolved_names[:50],
        "output": str(out_path),
    }
    print(json.dumps(summary))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
