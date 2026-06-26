"""Fetch cheque-return entries (Bank Payment vouchers booked against Sundry
Debtors) from Tally Prime via XML over HTTP.

A "cheque return" inside Tally is a Bank Payment voucher whose party row is
a Sundry Debtor — i.e. when a customer's cheque bounces, the original Bank
Receipt is reversed by debiting that debtor through a Bank Payment. So the
filter pipeline is:

    voucher type PARENT in {"payment", "payments"}            (bank/cash/JV
                                                               payment family)
    AND voucher type NAME contains "BANK"                     (bank payments
                                                               only — exclude
                                                               cash, JV)
    AND voucher's party LEDGERNAME is in the Sundry Debtors   (only debtor-
        ledger set for that company                            side payments)

For each surviving voucher, the script walks the party row's
``BILLALLOCATIONS.LIST`` and emits **one row per allocation** so the
``Reference Invoice Number`` column carries the original sales voucher whose
cheque bounced. Vouchers without bill allocations emit a single ``On
Account`` row (blank Ref Inv No).

Company / location values are taken from ``reference/companies.md`` so the
sheet shows clean display names instead of Tally's raw fiscal-year-stamped
names. Unmapped companies emit a one-time stderr warning and fall back to
the raw name with blank location.

Required env vars (loaded from project .env):
    TALLY_HOST   e.g. http://localhost:9000

Usage:
    python fetch_tally_chqreturn.py --from 01-04-2026 --to 25-04-2026 \\
        [--companies "Co1,Co2"] --output .tmp/chqreturn_<ts>.json
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


# Constant written to the Type column. Reference Excel uses "Bank Payment".
TYPE_LABEL = "Bank Payment"

# Parent classes that mark a voucher type as a payment transaction. Tally
# groups bank-payment, cash-payment, JV-payment etc. under PARENT 'Payment'
# (some companies use 'Payments'). We narrow further by name (must contain
# 'BANK') so cash payments and JV payments are excluded.
PAYMENT_PARENT_CLASSES: set[str] = {"payment", "payments"}

# The ledger-group name that flags a customer ledger. Tally's reserved
# primary group is exactly "Sundry Debtors". User-created sub-groups under
# it inherit the primary classification, so we walk the group hierarchy
# (PARENT chain) to determine membership instead of matching the immediate
# parent only.
SUNDRY_DEBTORS_PRIMARY = "Sundry Debtors"


def build_voucher_register_xml(company: str, from_date: str, to_date: str, vch_type: str) -> str:
    """Voucher Register query restricted to a single voucher type for the date range."""
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


def list_bank_payment_voucher_types(host: str, company: str) -> list[str]:
    """Return names of voucher types that are bank-side payments.

    Filter:
        PARENT in PAYMENT_PARENT_CLASSES  AND  NAME contains 'BANK'

    This catches "BANK PAYMENT", "BANK PAYMENTS", "Bank Payment" etc. while
    excluding "CASH PAYMENT", "JV PAYMENT", "PURCHASE PAYMENT" and any
    other payment subtype.
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
        if not name or parent.lower() not in PAYMENT_PARENT_CLASSES:
            continue
        if "bank" not in name.lower():
            continue
        out.append(name)
    seen: set[str] = set()
    return [n for n in out if not (n in seen or seen.add(n))]


def list_sundry_debtor_ledgers(host: str, company: str) -> set[str]:
    """Return the set of ledger names whose primary group is 'Sundry Debtors'.

    Walks the GROUP master once to build a child→parent map, then walks the
    LEDGER master and resolves each ledger's group chain up to a primary
    (root) group. A ledger qualifies if any group in its chain matches
    ``SUNDRY_DEBTORS_PRIMARY`` (case-insensitive). This handles user-defined
    sub-groups under Sundry Debtors (e.g. "Sundry Debtors - Domestic").
    """
    # Step 1 — fetch all groups with their parent.
    groups_body = f"""<ENVELOPE>
<HEADER><VERSION>1</VERSION><TALLYREQUEST>Export</TALLYREQUEST><TYPE>Collection</TYPE><ID>Grps</ID></HEADER>
<BODY><DESC>
<STATICVARIABLES>
<SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
<SVCURRENTCOMPANY>{_escape(company)}</SVCURRENTCOMPANY>
</STATICVARIABLES>
<TDL><TDLMESSAGE>
<COLLECTION NAME="Grps" ISMODIFY="No"><TYPE>Group</TYPE><FETCH>NAME,PARENT</FETCH></COLLECTION>
</TDLMESSAGE></TDL>
</DESC></BODY></ENVELOPE>"""
    resp = requests.post(host, data=groups_body.encode("utf-8"), timeout=60)
    if resp.status_code != 200:
        raise SystemExit(f"ERROR listing groups for '{company}': HTTP {resp.status_code}")
    cleaned = _sanitize_tally_xml(resp.content)
    root = ET.fromstring(cleaned)

    group_parent: dict[str, str] = {}
    for g in root.iter("GROUP"):
        name = (g.attrib.get("NAME") or _text(g.find("NAME"))).strip()
        parent = _text(g.find("PARENT"))
        if name:
            group_parent[name] = parent

    # Set of group names that resolve up to "Sundry Debtors" — the primary
    # group itself, plus any descendants. Cache results to avoid re-walking.
    debtor_groups: set[str] = set()
    cache: dict[str, bool] = {}
    target = SUNDRY_DEBTORS_PRIMARY.lower()

    def is_debtor_group(name: str, depth: int = 0) -> bool:
        if not name:
            return False
        if name in cache:
            return cache[name]
        if depth > 50:  # cycle guard, Tally groups are at most a few levels deep
            cache[name] = False
            return False
        if name.lower() == target:
            cache[name] = True
            return True
        parent = group_parent.get(name, "")
        result = is_debtor_group(parent, depth + 1) if parent else False
        cache[name] = result
        return result

    for g in group_parent:
        if is_debtor_group(g):
            debtor_groups.add(g)

    # Step 2 — fetch all ledgers with their immediate group; keep those whose
    # group is in debtor_groups.
    ledgers_body = f"""<ENVELOPE>
<HEADER><VERSION>1</VERSION><TALLYREQUEST>Export</TALLYREQUEST><TYPE>Collection</TYPE><ID>Ldgrs</ID></HEADER>
<BODY><DESC>
<STATICVARIABLES>
<SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
<SVCURRENTCOMPANY>{_escape(company)}</SVCURRENTCOMPANY>
</STATICVARIABLES>
<TDL><TDLMESSAGE>
<COLLECTION NAME="Ldgrs" ISMODIFY="No"><TYPE>Ledger</TYPE><FETCH>NAME,PARENT</FETCH></COLLECTION>
</TDLMESSAGE></TDL>
</DESC></BODY></ENVELOPE>"""
    resp = requests.post(host, data=ledgers_body.encode("utf-8"), timeout=60)
    if resp.status_code != 200:
        raise SystemExit(f"ERROR listing ledgers for '{company}': HTTP {resp.status_code}")
    cleaned = _sanitize_tally_xml(resp.content)
    root = ET.fromstring(cleaned)

    debtors: set[str] = set()
    for led in root.iter("LEDGER"):
        name = (led.attrib.get("NAME") or _text(led.find("NAME"))).strip()
        parent = _text(led.find("PARENT"))
        if name and parent in debtor_groups:
            debtors.add(name)
    return debtors


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

    Used to stamp a stable Tally GUID (``ledger_id``) onto each cheque-return row
    so customer identity survives ledger renames (the name text can change; the
    GUID never does). See ``scripts/LEDGER_ID_MIGRATION.md`` in the receivables repo.

    **Best effort:** on ANY failure this returns ``{}`` and the caller falls back
    to a blank ``ledger_id``. It must never break the daily fetch — the GUID is an
    additive enrichment, not a hard dependency.
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
    """Fetch all bank-payment vouchers for one company in the date range."""
    payment_types = list_bank_payment_voucher_types(host, company)
    all_vouchers: list[ET.Element] = []
    for vch_type in payment_types:
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
    Excel always shows the cheque-return amount as a positive Debit, so
    strip the sign.
    """
    if not amount_str:
        return ""
    s = amount_str.strip()
    if s.startswith(("-", "+")):
        s = s[1:]
    return s.strip()


def _party_ledger_entry(voucher: ET.Element, party: str) -> ET.Element | None:
    """Find the party's row in LEDGERENTRIES.LIST.

    For a Bank Payment booked against a debtor, the party row is the debit
    (positive) entry. Match by ``LEDGERNAME == party``; fall back to the
    largest absolute amount if name match fails.
    """
    candidates = list(voucher.findall("LEDGERENTRIES.LIST")) + list(voucher.findall("ALLLEDGERENTRIES.LIST"))
    if not candidates:
        return None
    if party:
        for led in candidates:
            if _text(led.find("LEDGERNAME")) == party:
                return led
    best: ET.Element | None = None
    best_val = -1.0
    for led in candidates:
        amt = _text(led.find("AMOUNT"))
        try:
            v = abs(float(amt.replace(",", "")))
        except ValueError:
            continue
        if v > best_val:
            best_val = v
            best = led
    return best


def _voucher_id(voucher: ET.Element) -> str:
    """Best-effort unique identifier. Bank Payment vouchers in this Tally
    setup may or may not carry a VOUCHERNUMBER — fall back to GUID and
    finally to the REMOTEID attribute. One of these is always present.
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


def _voucher_to_rows(voucher: ET.Element, company: str, location: str,
                     guid_map: dict[str, str] | None = None) -> list[dict[str, Any]]:
    """Emit one row per bill allocation; one ``On Account`` row if there are none.

    Caller is responsible for filtering to debtor parties before this fn is
    invoked.
    """
    date_raw = _text(voucher.find("DATE"))
    voucher_type_name = _text(voucher.find("VOUCHERTYPENAME"))
    voucher_no = _voucher_id(voucher)
    party = _text(voucher.find("PARTYLEDGERNAME")) or _text(voucher.find("PARTYNAME"))

    guid_map = guid_map or {}
    base: dict[str, Any] = {
        "company": company,
        "location": location,
        "month": derive_month(date_raw),
        "type": TYPE_LABEL,
        "date": format_date(date_raw),
        "particulars": party,
        "voucher_type": voucher_type_name,
        "voucher_no": voucher_no,
        "credit": "",  # always blank for cheque-return rows
        # Resolve the party name → GUID (ledger_id); all rows from this voucher
        # share the party, so stamp once on the base (LEDGER_ID_MIGRATION.md).
        "ledger_id": guid_map.get(party.strip().upper(), ""),
    }

    party_entry = _party_ledger_entry(voucher, party)
    allocations: list[ET.Element] = []
    if party_entry is not None:
        allocations = [a for a in party_entry.findall("BILLALLOCATIONS.LIST") if _is_real_allocation(a)]

    rows: list[dict[str, Any]] = []
    if allocations:
        for alloc in allocations:
            ref_name = _text(alloc.find("NAME"))
            amt = _text(alloc.find("AMOUNT"))
            row = dict(base)
            row["ref_inv_no"] = ref_name
            row["debit"] = _amount_abs(amt)
            rows.append(row)
    else:
        amt = ""
        if party_entry is not None:
            amt = _text(party_entry.find("AMOUNT"))
        row = dict(base)
        row["ref_inv_no"] = ""
        row["debit"] = _amount_abs(amt)
        rows.append(row)
    return rows


def _voucher_party(voucher: ET.Element) -> str:
    return _text(voucher.find("PARTYLEDGERNAME")) or _text(voucher.find("PARTYNAME"))


def project_columns(rows: Iterable[dict[str, Any]], columns: list[Column]) -> list[dict[str, Any]]:
    keys = [c.key for c in columns]
    out: list[dict[str, Any]] = []
    for r in rows:
        d = {k: r.get(k, "") for k in keys}
        # Carry the GUID as an extra field even though it is not (yet) a column
        # in reference/columns.md. The push tool only writes columns.md columns,
        # so this is invisible to the sheet / daily sync until ledger_id is
        # promoted to a real column at the migration gate.
        if "ledger_id" in r:
            d["ledger_id"] = r.get("ledger_id", "")
        out.append(d)
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="Fetch Tally cheque-return entries (Bank Payments against Sundry Debtors) as JSON.")
    parser.add_argument("--from", dest="from_date", required=True, help="Start date DD-MM-YYYY")
    parser.add_argument("--to", dest="to_date", required=True, help="End date DD-MM-YYYY")
    parser.add_argument("--companies", default="", help="Comma-separated subset of loaded companies (default: all)")
    parser.add_argument("--output", required=True, help="Output JSON path (e.g. .tmp/chqreturn_<ts>.json)")
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
    kept_count = 0
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

        # Per-company set of debtor ledgers — built once, applied to every
        # voucher to filter out vendor / expense / bank-side payments.
        debtors = list_sundry_debtor_ledgers(host, raw_company)
        # Per-company name→GUID map (best effort; blank GUIDs if it fails).
        guid_map = fetch_ledger_guid_map(host, raw_company)

        vouchers = fetch_company_vouchers(host, raw_company, from_date, to_date)
        voucher_count += len(vouchers)
        for v in vouchers:
            party = _voucher_party(v)
            if party not in debtors:
                continue
            kept_count += 1
            all_rows.extend(_voucher_to_rows(v, company=display_company, location=location, guid_map=guid_map))

    rows = project_columns(all_rows, columns)

    # ledger_id resolution stats (D5 visibility).
    named = [r for r in all_rows if (r.get("particulars") or "").strip()]
    resolved = sum(1 for r in named if (r.get("ledger_id") or "").strip())
    unresolved = len(named) - resolved
    unresolved_names = sorted({
        r["particulars"].strip() for r in named if not (r.get("ledger_id") or "").strip()
    })

    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(rows, indent=2, ensure_ascii=False), encoding="utf-8")

    summary = {
        "vouchers_scanned": voucher_count,
        "vouchers_against_debtors": kept_count,
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
