"""Fetch journal vouchers (booked against Sundry Debtors) from Tally Prime
via XML over HTTP.

Loops over each loaded company (or a user-supplied subset), pulls all
voucher types whose PARENT class is ``Journal``, then for each voucher
filters to **only** the LEDGERENTRIES legs whose ledger rolls up to
``Sundry Debtors``. Emits one output row per debtor leg (so a journal
that posts against two debtors yields two rows; one against zero debtors
yields none).

Each row carries a ``Transaction Type`` of ``Dr`` or ``Cr`` reflecting
which side that debtor leg sits on:

* ``Dr`` — receivable increased (party debited).
* ``Cr`` — receivable reduced (party credited).

Tally serialises the leg amount with a leading ``-`` for debits and no
sign for credits in the LEDGERENTRIES.LIST AMOUNT field. We use
``ISDEEMEDPOSITIVE`` as the primary signal (``Yes`` = Dr) and the AMOUNT
sign as a fallback; ``Amount`` in the output is always the unsigned
absolute value.

The "Against Sales Invoice no." column comes from the voucher-level
``<REFERENCE>`` field — same convention as the credit/debit-note skills.
Many journals don't carry a REFERENCE; those rows show blank.

Company / location values are taken from ``reference/companies.md``.

Required env vars (loaded from project .env):
    TALLY_HOST   e.g. http://localhost:9000

Usage:
    python fetch_tally_journals.py --from 01-04-2026 --to 25-04-2026 \\
        [--companies "Co1,Co2"] --output .tmp/journals_<ts>.json
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


TYPE_LABEL = "journal"

# Parent classes that mark a voucher type as a journal. Tally's predefined
# class is exactly "Journal"; some sites use "Journals" (plural) so we
# accept both.
JOURNAL_PARENT_CLASSES: set[str] = {"journal", "journals"}

# Primary groups whose descendants we keep. A voucher leg is emitted into
# the sheet only if its ledger rolls up to one of these primaries.
#
# - ``Sundry Debtors``: external customers + ``BALANCE WITH RELATED PARTY(Debtors)``
#   sub-group (which the user's accountant has placed under Sundry Debtors).
# - ``Branch / Divisions``: inter-company / inter-branch ledgers (Surat ↔ Noida ↔
#   Delhi ↔ Mumbai ↔ ISD). Necessary because inter-branch journal legs (e.g.
#   ``Dr ORANGE O TEC NOIDA`` paired against a Cr to a debtor) are part of the
#   same voucher and need to flow into the sheet so the journal balances when
#   read row-by-row. ``BALANCE WITH RELATED PARTY`` (without ``(Debtors)``) is
#   intentionally NOT included — it rolls up to Sundry Creditors (a payable
#   group) and doesn't belong on a receivables sheet.
TARGET_PRIMARY_GROUPS: tuple[str, ...] = ("Sundry Debtors", "Branch / Divisions")


def build_voucher_register_xml(company: str, from_date: str, to_date: str, vch_type: str) -> str:
    """Voucher Register query restricted to a single voucher type for the date range.

    Same envelope as the sibling credit/debit-note skills — the older-style
    ``<TALLYREQUEST>Export Data</TALLYREQUEST>`` + ``<EXPORTDATA><REQUESTDESC>``
    form is required for Voucher Register to honour ``VOUCHERTYPENAME``.
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


def list_journal_voucher_types(host: str, company: str) -> list[str]:
    """Return names of all voucher types whose PARENT is in
    ``JOURNAL_PARENT_CLASSES`` for the given company.

    The Sundry-Debtor party-leg filter (applied later) is the authoritative
    cut, so we don't pre-filter voucher type names here.
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
        if name and parent.lower() in JOURNAL_PARENT_CLASSES:
            out.append(name)
    seen: set[str] = set()
    return [n for n in out if not (n in seen or seen.add(n))]


def list_target_ledgers(host: str, company: str) -> set[str]:
    """Return ledgers whose primary group is in ``TARGET_PRIMARY_GROUPS``.

    Walks the GROUP master once to build a child→parent map, then walks the
    LEDGER master and resolves each ledger's group chain up to a primary
    (root) group. A ledger qualifies if any group in its chain matches one
    of the configured primaries (case-insensitive). Same group-chain walk
    pattern as tally-chequereturn-sync-outstanding and tally-salesdebitnote-sync-outstanding,
    generalised to multiple primary groups so this skill can pick up
    inter-branch ledgers under ``Branch / Divisions`` in addition to the
    Sundry-Debtor ledgers under ``Sundry Debtors``.
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

    target_groups: set[str] = set()
    cache: dict[str, bool] = {}
    targets_lower = {p.lower() for p in TARGET_PRIMARY_GROUPS}

    def is_target_group(name: str, depth: int = 0) -> bool:
        if not name:
            return False
        if name in cache:
            return cache[name]
        if depth > 50:
            cache[name] = False
            return False
        if name.lower() in targets_lower:
            cache[name] = True
            return True
        parent = group_parent.get(name, "")
        result = is_target_group(parent, depth + 1) if parent else False
        cache[name] = result
        return result

    for g in group_parent:
        if is_target_group(g):
            target_groups.add(g)

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

    targets: set[str] = set()
    for led in root.iter("LEDGER"):
        name = (led.attrib.get("NAME") or _text(led.find("NAME"))).strip()
        parent = _text(led.find("PARENT"))
        if name and parent in target_groups:
            targets.add(name)
    return targets


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

    Used to stamp a stable Tally GUID (``ledger_id``) onto each journal-leg row
    so identity survives ledger renames (the name text can change; the GUID never
    does). See ``scripts/LEDGER_ID_MIGRATION.md`` in the receivables repo.

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
    """Fetch all journal vouchers for one company in the date range."""
    types = list_journal_voucher_types(host, company)
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
    if not amount_str:
        return ""
    s = amount_str.strip().lstrip("+").lstrip("-").strip()
    return s


def _amount_sign(amount_str: str) -> int:
    """Return -1 for negative, 1 for positive, 0 for empty/unparseable."""
    if not amount_str:
        return 0
    try:
        v = float(amount_str.replace(",", "").strip())
    except ValueError:
        return 0
    if v < 0:
        return -1
    if v > 0:
        return 1
    return 0


def _leg_dr_cr(leg: ET.Element) -> str:
    """Determine whether this LEDGERENTRIES.LIST leg is a Dr or Cr posting.

    Tally's convention in voucher XML:
        ISDEEMEDPOSITIVE = "Yes"  → debit  (Dr)
        ISDEEMEDPOSITIVE = "No"   → credit (Cr)
        AMOUNT carries a leading '-' for Dr legs, no sign for Cr legs.

    We trust ISDEEMEDPOSITIVE first; fall back to AMOUNT sign if absent.
    """
    flag = _text(leg.find("ISDEEMEDPOSITIVE")).lower()
    if flag in {"yes", "true", "1"}:
        return "Dr"
    if flag in {"no", "false", "0"}:
        return "Cr"
    sign = _amount_sign(_text(leg.find("AMOUNT")))
    if sign < 0:
        return "Dr"
    if sign > 0:
        return "Cr"
    return ""


def _is_real_allocation(alloc: ET.Element) -> bool:
    """Tally emits an empty ``<BILLALLOCATIONS.LIST/>`` (zero children) on
    On-Account legs instead of omitting the element. Treat those as
    "no allocation" so the On-Account branch handles them. Same pattern
    as tally-chequereturn-sync-outstanding / tally-bankreceipt-sync-outstanding.
    """
    return bool(_text(alloc.find("NAME")) or _text(alloc.find("BILLTYPE")) or _text(alloc.find("AMOUNT")))


def _voucher_id(voucher: ET.Element) -> str:
    """Best-effort unique identifier. Journal vouchers in this Tally setup
    often have no VOUCHERNUMBER (Tally doesn't assign one for manual
    journals unless the voucher type is configured to). Fall back to GUID
    and finally to the REMOTEID attribute. One of these is always present.
    Same pattern as the sibling tally-chequereturn-sync-outstanding skill.
    """
    for tag in ("VOUCHERNUMBER", "GUID"):
        v = _text(voucher.find(tag))
        if v:
            return v
    return voucher.attrib.get("REMOTEID", "").strip()


def _kept_legs(voucher: ET.Element, target_ledgers: set[str]) -> list[ET.Element]:
    """Return every LEDGERENTRIES leg whose ledger is in the target set
    (Sundry Debtors ∪ Branch / Divisions). Inter-branch legs are kept so
    that journals like ``Dr ORANGE O TEC NOIDA / Cr J.P. PROCESSORS`` flow
    in as both rows — letting each voucher's Dr and Cr sides reconcile
    when the sheet is read row-by-row.
    """
    legs: list[ET.Element] = []
    for led in list(voucher.findall("LEDGERENTRIES.LIST")) + list(voucher.findall("ALLLEDGERENTRIES.LIST")):
        name = _text(led.find("LEDGERNAME"))
        if name and name in target_ledgers:
            legs.append(led)
    return legs


def voucher_to_rows(voucher: ET.Element, company: str, location: str, target_ledgers: set[str],
                    guid_map: dict[str, str] | None = None) -> list[dict[str, Any]]:
    """Emit one row per BILLALLOCATIONS entry under each kept leg.

    A "kept leg" is a LEDGERENTRIES leg whose ledger rolls up to one of
    ``TARGET_PRIMARY_GROUPS`` (Sundry Debtors or Branch / Divisions). A
    journal that posts a single ₹10L credit against six original invoices
    for the same debtor produces six rows — each carries that allocation's
    Reference Invoice Number and partial amount. Same fan-out pattern as
    the bank-receipt and cheque-return skills.

    Vouchers with no kept leg are dropped entirely. Kept legs with no bill
    allocations (or empty ``<BILLALLOCATIONS.LIST/>``) emit one row with
    blank ``ref_inv_no`` and the full leg amount — an "On Account"
    posting. Inter-branch legs typically carry no allocations, so they
    surface as On Account rows.
    """
    legs = _kept_legs(voucher, target_ledgers)
    if not legs:
        return []

    date_raw = _text(voucher.find("DATE"))
    voucher_type_name = _text(voucher.find("VOUCHERTYPENAME"))
    voucher_no = _voucher_id(voucher)
    narration = _text(voucher.find("NARRATION"))

    base: dict[str, Any] = {
        "company": company,
        "location": location,
        "month": derive_month(date_raw),
        "type": TYPE_LABEL,
        "date": format_date(date_raw),
        "voucher_type": voucher_type_name,
        "voucher_no": voucher_no,
        "narration": narration,
    }

    guid_map = guid_map or {}
    rows: list[dict[str, Any]] = []
    for leg in legs:
        leg_base = dict(base)
        leg_base["particulars"] = _text(leg.find("LEDGERNAME"))
        leg_base["transaction_type"] = _leg_dr_cr(leg)
        # Resolve the LEG's ledger name → GUID (ledger_id). Each kept leg (debtor
        # or branch ledger) is its own identity; rows from this leg inherit it.
        leg_base["ledger_id"] = guid_map.get(leg_base["particulars"].strip().upper(), "")

        allocations = [a for a in leg.findall("BILLALLOCATIONS.LIST") if _is_real_allocation(a)]
        if allocations:
            for alloc in allocations:
                row = dict(leg_base)
                row["ref_inv_no"] = _text(alloc.find("NAME"))
                row["amount"] = _amount_abs(_text(alloc.find("AMOUNT")))
                rows.append(row)
        else:
            row = dict(leg_base)
            row["ref_inv_no"] = ""
            row["amount"] = _amount_abs(_text(leg.find("AMOUNT")))
            rows.append(row)
    return rows


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
    parser = argparse.ArgumentParser(description="Fetch Tally journal vouchers (against Sundry Debtors) as JSON.")
    parser.add_argument("--from", dest="from_date", required=True, help="Start date DD-MM-YYYY")
    parser.add_argument("--to", dest="to_date", required=True, help="End date DD-MM-YYYY")
    parser.add_argument("--companies", default="", help="Comma-separated subset of loaded companies (default: all)")
    parser.add_argument("--output", required=True, help="Output JSON path (e.g. .tmp/journals_<ts>.json)")
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
    kept_voucher_count = 0
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

        target_ledgers = list_target_ledgers(host, raw_company)
        # Per-company name→GUID map (best effort; blank GUIDs if it fails).
        guid_map = fetch_ledger_guid_map(host, raw_company)

        vouchers = fetch_company_vouchers(host, raw_company, from_date, to_date)
        voucher_count += len(vouchers)
        for v in vouchers:
            rows = voucher_to_rows(v, company=display_company, location=location,
                                   target_ledgers=target_ledgers, guid_map=guid_map)
            if rows:
                kept_voucher_count += 1
                all_rows.extend(rows)

    rows = project_columns(all_rows, columns)

    # ledger_id resolution stats (D5 visibility). Party = each leg's ledger name.
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
        "vouchers_with_kept_leg": kept_voucher_count,
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
