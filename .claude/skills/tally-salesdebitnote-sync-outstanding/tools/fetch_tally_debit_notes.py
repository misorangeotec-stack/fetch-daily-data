"""Fetch sales debit-note vouchers (booked against Sundry Debtors) from Tally
Prime via XML over HTTP.

Loops over each loaded company (or a user-supplied subset), pulls all
debit-note vouchers between --from and --to dates, **filters to only
vouchers whose party ledger rolls up to "Sundry Debtors"**, aggregates
inventory lines into one row per voucher, and writes a JSON list-of-dicts
whose keys match the schema in ``reference/columns.md``.

A "debit note" here is any voucher whose voucher-type PARENT is exactly
``Debit Note``. That covers SALES DEBIT NOTE, GST DEBIT NOTE,
GST DEBIT NOTE-ISD, GST DEBIT NOTE (23-24), DEBIT NOTE-194R FOC,
TCS DEBIT NOTE, and Tally's predefined ``Debit Note``. Voucher types whose
name contains ``purchase``, ``purch``, or ``return`` are skipped early —
those are vendor-side adjustments that don't belong on a receivables sheet.
The party-side Sundry-Debtor filter is the authoritative second stage —
any voucher whose party isn't a Sundry Debtor is dropped regardless of
voucher type, so cross-classified vouchers can't pollute the sheet.

The "Against Sales Invoice no." column comes from the voucher-level
``<REFERENCE>`` field — same convention as the sibling sales-credit-note
skill. Many sales-side debit notes (interest, late fees, ISD reversals)
have no original invoice; those rows show blank, which is expected.

Company / location values are taken from ``reference/companies.md`` so the
sheet shows clean display names instead of Tally's raw fiscal-year-stamped
names. Unmapped companies emit a one-time stderr warning and fall back to
the raw name with blank location.

Required env vars (loaded from project .env):
    TALLY_HOST   e.g. http://localhost:9000

Usage:
    python fetch_tally_debit_notes.py --from 01-04-2026 --to 25-04-2026 \\
        [--companies "Co1,Co2"] --output .tmp/debit_notes_<ts>.json
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


# Constant written to the Type column. Mirrors the credit-note skill's
# convention of using a lowercase singular label regardless of underlying
# voucher type.
TYPE_LABEL = "debit note"

# Parent classes that mark a voucher type as a debit note. Only one parent
# was found in the live Tally probe ("Debit Note") but the set keeps the
# matching code the same shape as the sibling skills.
DEBIT_NOTE_PARENT_CLASSES: set[str] = {"debit note"}

# Voucher-type names containing any of these substrings are skipped at the
# voucher-type listing stage (saves a Tally roundtrip per voucher). The
# party-side filter is the authoritative one, but dropping these here
# avoids fetching N obviously-purchase-side vouchers we'd discard anyway.
# NOTE: "isd" is intentionally NOT in this list — GST DEBIT NOTE-ISD can
# legitimately be raised against a debtor, in which case the party filter
# keeps it.
SKIP_NAME_SUBSTRINGS: tuple[str, ...] = ("purchase", "purch", "return")

# Primary group whose descendants are debtor ledgers. The walk is the same
# pattern used by tally-chequereturn-sync-outstanding.
SUNDRY_DEBTORS_PRIMARY = "Sundry Debtors"


def build_voucher_register_xml(company: str, from_date: str, to_date: str, vch_type: str) -> str:
    """Voucher Register query restricted to a single voucher type for the date range.

    Same pattern as the sibling credit-note / sales skills — Tally's
    built-in 'Debit Note Register' filters to one master voucher type per
    company, so we enumerate types via PARENT='Debit Note' and run one
    Voucher Register query per type. Voucher Register reliably honours
    ``VOUCHERTYPENAME`` as a filter. Dates are in Tally's ``YYYYMMDD`` form.
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


def list_debit_note_voucher_types(host: str, company: str) -> list[str]:
    """Return names of voucher types under PARENT='Debit Note' minus the
    obviously-purchase-side ones (PURCHASE DEBIT NOTE, PURCHASE RETURN).
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
        if not name or parent.lower() not in DEBIT_NOTE_PARENT_CLASSES:
            continue
        lower = name.lower()
        if any(sub in lower for sub in SKIP_NAME_SUBSTRINGS):
            continue
        out.append(name)
    seen: set[str] = set()
    return [n for n in out if not (n in seen or seen.add(n))]


def list_sundry_debtor_ledgers(host: str, company: str) -> set[str]:
    """Return ledgers whose primary group is 'Sundry Debtors'.

    Walks the GROUP master once to build a child→parent map, then walks the
    LEDGER master and resolves each ledger's group chain up to a primary
    (root) group. A ledger qualifies if any group in its chain matches
    ``SUNDRY_DEBTORS_PRIMARY`` (case-insensitive). Handles user-defined
    sub-groups under Sundry Debtors (e.g. "Sundry Debtors - Domestic").
    Identical pattern to tally-chequereturn-sync-outstanding.
    """
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

    debtor_groups: set[str] = set()
    cache: dict[str, bool] = {}
    target = SUNDRY_DEBTORS_PRIMARY.lower()

    def is_debtor_group(name: str, depth: int = 0) -> bool:
        if not name:
            return False
        if name in cache:
            return cache[name]
        if depth > 50:
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

    Used to stamp a stable Tally GUID (``ledger_id``) onto each debit-note row so
    customer identity survives ledger renames (the name text can change; the GUID
    never does). See ``scripts/LEDGER_ID_MIGRATION.md`` in the receivables repo.

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
    """Fetch all debit-note vouchers (after name filter) for one company in the date range."""
    types = list_debit_note_voucher_types(host, company)
    all_vouchers: list[ET.Element] = []
    for vch_type in types:
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


_QTY_SPLIT_RE = re.compile(r"^\s*(-?[\d,]*\.?\d+)\s*(\S.*)?$")


def split_qty_unit(actual_qty: str) -> tuple[str, str]:
    """Split a Tally ACTUALQTY string like '10 KG' into ('10', 'KG')."""
    if not actual_qty:
        return "", ""
    m = _QTY_SPLIT_RE.match(actual_qty)
    if not m:
        return actual_qty.strip(), ""
    qty = m.group(1).replace(",", "")
    unit = (m.group(2) or "").strip()
    return qty, unit


def derive_month(date_yyyymmdd: str) -> str:
    if not date_yyyymmdd or len(date_yyyymmdd) < 6:
        return ""
    try:
        return datetime.strptime(date_yyyymmdd, "%Y%m%d").strftime("%Y-%m")
    except ValueError:
        return ""


def format_date(date_yyyymmdd: str) -> str:
    """Render the date as DD/MM/YYYY (Indian convention, sortable as a date)."""
    if not date_yyyymmdd or len(date_yyyymmdd) < 8:
        return ""
    try:
        return datetime.strptime(date_yyyymmdd, "%Y%m%d").strftime("%d/%m/%Y")
    except ValueError:
        return date_yyyymmdd


def _voucher_gross_total(voucher: ET.Element, party: str) -> str:
    """Voucher's gross total as a positive string.

    Same lookup as the credit-note skill: find the party in
    LEDGERENTRIES.LIST and take ``abs(AMOUNT)``. For a debit note booked
    against a debtor the party row's AMOUNT is negative (the customer's
    receivable goes up, recorded as a debit on the party ledger which Tally
    serialises with a leading minus). ``abs()`` normalises it.
    """
    candidates = list(voucher.findall("LEDGERENTRIES.LIST")) + list(voucher.findall("ALLLEDGERENTRIES.LIST"))
    if not candidates:
        return ""
    if party:
        for led in candidates:
            if _text(led.find("LEDGERNAME")) == party:
                amt = _text(led.find("AMOUNT"))
                if amt:
                    return amt.lstrip("-").lstrip("+").strip()
    best = ""
    best_val = -1.0
    for led in candidates:
        amt = _text(led.find("AMOUNT"))
        if not amt:
            continue
        try:
            v = abs(float(amt.replace(",", "")))
        except ValueError:
            continue
        if v > best_val:
            best_val = v
            best = amt.lstrip("-").lstrip("+").strip()
    return best


def _aggregate_inventory(voucher: ET.Element) -> dict[str, str]:
    """Collapse all inventory lines into single quantity / rate / value fields.

    Returns blanks when the voucher has no inventory lines (the common case
    for interest, late-fee, ISD, and rate-difference debit notes).
    """
    invs = list(voucher.findall("ALLINVENTORYENTRIES.LIST"))
    if not invs:
        return {"quantity": "", "rate": "", "value": ""}

    qty_sum = 0.0
    val_sum = 0.0
    has_qty = False
    has_val = False

    for inv in invs:
        actual_qty = _text(inv.find("ACTUALQTY")) or _text(inv.find("BILLEDQTY"))
        qty_str, _unit = split_qty_unit(actual_qty)
        try:
            qty_sum += float(qty_str.replace(",", ""))
            has_qty = True
        except ValueError:
            pass
        amt = _text(inv.find("AMOUNT"))
        try:
            val_sum += float(amt.replace(",", ""))
            has_val = True
        except ValueError:
            pass

    quantity = f"{abs(qty_sum):.4f}" if has_qty else ""
    value = f"{abs(val_sum):.2f}" if has_val else ""
    rate = f"{abs(val_sum / qty_sum):.2f}" if has_qty and qty_sum != 0 and has_val else ""
    return {"quantity": quantity, "rate": rate, "value": value}


def voucher_to_row(
    voucher: ET.Element,
    company: str,
    location: str,
    guid_map: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Build one aggregated row per debit-note voucher."""
    date_raw = _text(voucher.find("DATE"))
    voucher_type_name = _text(voucher.find("VOUCHERTYPENAME"))
    voucher_no = _text(voucher.find("VOUCHERNUMBER"))
    party = _text(voucher.find("PARTYLEDGERNAME")) or _text(voucher.find("PARTYNAME"))
    against_invoice = _text(voucher.find("REFERENCE"))
    narration = _text(voucher.find("NARRATION"))
    gross_total = _voucher_gross_total(voucher, party)

    row: dict[str, Any] = {
        "company": company,
        "location": location,
        "month": derive_month(date_raw),
        "type": TYPE_LABEL,
        "date": format_date(date_raw),
        "particulars": party,
        "voucher_type": voucher_type_name,
        "voucher_no": voucher_no,
        "against_invoice": against_invoice,
        "narration": narration,
        "gross_total": gross_total,
    }
    row.update(_aggregate_inventory(voucher))
    # Resolve the party name → GUID (ledger_id). Display-only name (particulars)
    # stays on the row; identity will key on this GUID (LEDGER_ID_MIGRATION.md).
    guid_map = guid_map or {}
    row["ledger_id"] = guid_map.get(party.strip().upper(), "")
    return row


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
    parser = argparse.ArgumentParser(description="Fetch Tally debit-note vouchers (against Sundry Debtors) as JSON.")
    parser.add_argument("--from", dest="from_date", required=True, help="Start date DD-MM-YYYY")
    parser.add_argument("--to", dest="to_date", required=True, help="End date DD-MM-YYYY")
    parser.add_argument("--companies", default="", help="Comma-separated subset of loaded companies (default: all)")
    parser.add_argument("--output", required=True, help="Output JSON path (e.g. .tmp/debit_notes_<ts>.json)")
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
            all_rows.append(voucher_to_row(v, company=display_company, location=location, guid_map=guid_map))

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
