"""Fetch sales vouchers from Tally Prime via XML over HTTP.

Loops over each loaded company (or a user-supplied subset), pulls all sales
vouchers between --from and --to dates, and emits **two parallel row sets**
per fetch in a single JSON file:

* ``vouchers`` — one row per voucher (inventory lines aggregated). Schema:
  ``reference/columns.md``. Pushed to the per-voucher Sales sheet.
* ``details``  — one row per ``BILLALLOCATIONS.LIST`` entry under each
  voucher (with computed Due Date = voucher date + credit period). Vouchers
  with no real allocation emit a synthetic ``On Account`` row carrying the
  full gross total. Schema: ``reference/columns_details.md``. Pushed to the
  bill-wise Sales Outstanding Register.

Joinable on ``(Company, Location, Voucher No.)`` between the two row sets.

Output JSON shape::

    {"vouchers": [...], "details": [...]}

(For backward-compat readers, ``push_sales_to_sheet.py`` also accepts the
legacy flat-list form — a bare list is treated as ``vouchers`` only.)

Company / location values are taken from ``reference/companies.md`` so the
sheet shows clean display names instead of Tally's raw fiscal-year-stamped
names. Unmapped companies emit a one-time stderr warning and fall back to
the raw name with blank location.

Required env vars (loaded from project .env):
    TALLY_HOST   e.g. http://localhost:9000

Usage:
    python fetch_tally_sales.py --from 01-04-2026 --to 25-04-2026 \\
        [--companies "Co1,Co2"] --output .tmp/sales_<ts>.json
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

import requests
from dotenv import load_dotenv

# Make the shared loaders importable when this script is invoked directly.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _schema import Column, load_columns, load_companies, load_details_columns  # noqa: E402

from list_tally_companies import list_companies  # noqa: E402


def build_voucher_register_xml(company: str, from_date: str, to_date: str, vch_type: str) -> str:
    """Voucher Register query restricted to a single voucher type for the date range.

    The Tally 'Sales Vouchers' report is unreliable — it returns vouchers of
    only one specific master type per company, ignoring others. Instead we
    enumerate every voucher type whose PARENT is 'Sales' (via
    ``list_sales_voucher_types``) and run one Voucher Register query per
    type. Voucher Register reliably honours ``VOUCHERTYPENAME`` as a filter.
    Dates are in Tally's ``YYYYMMDD`` wire form.
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


# Parent classes that mark a voucher type as a sales transaction. Tally lets
# each company pick which "predefined class" to use — most use 'Sales', but
# some use 'Sales Accounts' (extra word) and Colorix's company uses 'GST SALES'
# as its sales class (verified: no other company parents non-sales types to it,
# so this is safe to include). 'Sales Order' is NOT here: orders are commitments,
# not booked sales, and shouldn't appear in an outstanding (receivables) report.
#
# O-tec/Surat (and others) use a suffixed variant 'Sales Accounts-HSS' as the
# sales class, so we ALSO accept any parent that starts with 'sales accounts'
# (see is_sales_parent). 2026-06-20: this exact-set miss returned 0 sales types
# for O-tec/Surat → 0 sales fetched → a heal pure-deleted real sales. 'Sales Order'
# does NOT start with 'sales accounts', so it stays excluded.
SALES_PARENT_CLASSES: set[str] = {"sales", "sales accounts", "gst sales"}


def is_sales_parent(parent: str) -> bool:
    """True if a voucher type's PARENT class marks it as booked sales."""
    p = (parent or "").strip().lower()
    return p in SALES_PARENT_CLASSES or p.startswith("sales accounts")


def list_sales_voucher_types(host: str, company: str) -> list[str]:
    """Return names of all voucher types whose PARENT is a sales class for
    the given company (see ``SALES_PARENT_CLASSES``).

    Tally allows users to define many sales voucher types ('GST SALES - INK',
    'GST SALES - MACHINE', etc.) — they all share the same parent class.
    This function returns every type the company actually has so the fetch
    loop can hit each one explicitly. Includes types with naming variations
    or typos (e.g. 'GST SALE- SPARE PARTS' singular).
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
        if name and is_sales_parent(parent):
            out.append(name)
    # Dedupe while preserving order
    seen: set[str] = set()
    return [n for n in out if not (n in seen or seen.add(n))]


def _escape(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def parse_dmy(s: str) -> str:
    """Convert DD-MM-YYYY → YYYYMMDD (Tally's wire format)."""
    return datetime.strptime(s, "%d-%m-%Y").strftime("%Y%m%d")


# Tally's XML output is messy: it mixes invalid UTF-8 bytes (stray Windows-1252
# chars), raw control bytes (0x00-0x1F), and numeric char refs like &#0; that
# all break stdlib ElementTree. Decode tolerantly, strip XML-illegal chars,
# then re-encode.
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

    Used to stamp a stable Tally GUID (``ledger_id``) onto each sales row so
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
    """Fetch all sales-category vouchers for one company in the date range.

    Enumerates every voucher type whose PARENT is 'Sales' in this company's
    master, then issues one Voucher Register query per type. Skips cancelled
    vouchers. No name-based filtering is needed — the upstream PARENT='Sales'
    check is the source of truth, so types like ``GST SALE- SPARE PARTS``
    (typo) are still captured.
    """
    sales_types = list_sales_voucher_types(host, company)
    all_vouchers: list[ET.Element] = []
    for vch_type in sales_types:
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
    """Skip cancelled or soft-deleted (optional) vouchers."""
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
    """Split a Tally ACTUALQTY string like '10 KG' into ('10', 'KG').

    Returns ('', '') if the field is empty. If only a number is present,
    returns ('<number>', '').
    """
    if not actual_qty:
        return "", ""
    m = _QTY_SPLIT_RE.match(actual_qty)
    if not m:
        return actual_qty.strip(), ""
    qty = m.group(1).replace(",", "")
    unit = (m.group(2) or "").strip()
    return qty, unit


def derive_type(voucher_type_name: str) -> str:
    """Sales.xlsx 'Type' column = the high-level group from VOUCHERTYPENAME.

    Example: 'GST SALES - HEAD' → 'GST SALES'.
    """
    if not voucher_type_name:
        return ""
    return voucher_type_name.split("-", 1)[0].strip()


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
    """Return the voucher's gross total (incl. taxes) as a positive string.

    Tally doesn't expose a single voucher-level total tag — it lives in the
    party's row of LEDGERENTRIES.LIST as a negative amount (the party owes
    the company). Falls back to the largest absolute amount across ledger
    entries if the party row can't be matched by name.
    """
    candidates = list(voucher.findall("LEDGERENTRIES.LIST")) + list(voucher.findall("ALLLEDGERENTRIES.LIST"))
    if not candidates:
        return ""
    if party:
        for led in candidates:
            if _text(led.find("LEDGERNAME")) == party:
                amt = _text(led.find("AMOUNT"))
                if amt:
                    return _amount_abs(amt)
    # Fallback: max abs(amount) across ledger entries
    best = ""
    best_val = -1.0
    for led in candidates:
        amt = _text(led.find("AMOUNT"))
        cleaned = _amount_abs(amt)
        if not cleaned:
            continue
        try:
            v = abs(float(cleaned.replace(",", "")))
        except ValueError:
            continue
        if v > best_val:
            best_val = v
            best = cleaned
    return best


def _aggregate_inventory(voucher: ET.Element) -> dict[str, str]:
    """Collapse all inventory lines of one voucher into single quantity / rate /
    unit / value fields.

    - quantity: numeric sum
    - value:    numeric sum
    - rate:     weighted avg = sum(value)/sum(quantity), 2dp; blank if qty=0
    - unit:     the shared unit if every line has the same one; blank otherwise

    Returns blank strings for all four fields when the voucher has no
    inventory lines (cash sales w/o stock items).
    """
    invs = list(voucher.findall("ALLINVENTORYENTRIES.LIST"))
    if not invs:
        return {"quantity": "", "rate": "", "unit": "", "value": ""}

    qty_sum = 0.0
    val_sum = 0.0
    units: set[str] = set()
    has_qty = False
    has_val = False

    for inv in invs:
        actual_qty = _text(inv.find("ACTUALQTY")) or _text(inv.find("BILLEDQTY"))
        qty_str, unit = split_qty_unit(actual_qty)
        if not unit:
            unit = _text(inv.find("BASEUNITS"))
        if unit:
            units.add(unit)
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

    quantity = f"{qty_sum:.4f}" if has_qty else ""
    value = f"{val_sum:.2f}" if has_val else ""
    rate = f"{(val_sum / qty_sum):.2f}" if has_qty and qty_sum != 0 and has_val else ""
    unit_str = next(iter(units)) if len(units) == 1 else ""
    return {"quantity": quantity, "rate": rate, "unit": unit_str, "value": value}


def _is_real_allocation(alloc: ET.Element) -> bool:
    """Tally emits an empty ``<BILLALLOCATIONS.LIST/>`` (zero children) on
    On-Account legs instead of omitting the element. Treat those as
    "no allocation" so the synthetic On-Account branch handles them. Same
    pattern as tally-salesjournal-sync-outstanding / tally-bankreceipt /
    tally-chequereturn.
    """
    return bool(_text(alloc.find("NAME")) or _text(alloc.find("BILLTYPE")) or _text(alloc.find("AMOUNT")))


_CREDIT_PERIOD_DAYS_RE = re.compile(r"^\s*(\d+)\s*(?:days?)?\s*$", re.IGNORECASE)
_CREDIT_PERIOD_DATE_RE = re.compile(r"^\s*(\d{1,2})[-/ ]([A-Za-z]{3,9}|\d{1,2})[-/ ](\d{2,4})\s*$")
_MONTH_TOKENS = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
    "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7,
    "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9,
    "oct": 10, "october": 10, "nov": 11, "november": 11, "dec": 12, "december": 12,
}


def _parse_credit_period(credit_period: str, voucher_date: "datetime.date | None") -> "tuple[int, datetime.date] | None":
    """Resolve Tally's BILLCREDITPERIOD into ``(days, due_date)``.

    Tally stores whatever the user typed in the "Due Date, or Credit Days"
    column — three forms appear in the wild:

    * Pure day count: ``"37"``, ``"37 Days"`` (case-insensitive). Compute
      ``due_date = voucher_date + days``.
    * Date: ``"17-Jul-25"``, ``"17/07/2025"``, ``"17 July 2025"``. Parse the
      date directly; back-compute ``days = (due_date - voucher_date).days``.
    * Empty / unparseable → return ``None``.

    A ``voucher_date`` of ``None`` is allowed only when the input is a full
    date — without a voucher date we can't compute days for the days form.
    Two-digit years are interpreted as 2000+yy (Tally always emits 2-digit
    years post-2000 for these fields).
    """
    if not credit_period:
        return None

    # Days form.
    m = _CREDIT_PERIOD_DAYS_RE.match(credit_period)
    if m:
        try:
            days = int(m.group(1))
        except ValueError:
            return None
        if voucher_date is None:
            return None
        return days, voucher_date + timedelta(days=days)

    # Date form (e.g. "17-Jul-25", "17/07/2025", "17 Jul 2025").
    m = _CREDIT_PERIOD_DATE_RE.match(credit_period)
    if not m:
        return None
    try:
        day_n = int(m.group(1))
        mon_raw = m.group(2)
        year_n = int(m.group(3))
    except ValueError:
        return None
    if year_n < 100:
        year_n += 2000
    # Month can be a name (Jul) or a number (07).
    if mon_raw.isdigit():
        mon_n = int(mon_raw)
    else:
        mon_n = _MONTH_TOKENS.get(mon_raw.lower(), 0)
    if not (1 <= mon_n <= 12 and 1 <= day_n <= 31):
        return None
    try:
        due = datetime(year_n, mon_n, day_n).date()
    except ValueError:
        return None
    if voucher_date is None:
        return 0, due
    return (due - voucher_date).days, due


def resolve_credit_period_and_due_date(credit_period: str, voucher_date_yyyymmdd: str) -> tuple[str, str]:
    """Return ``(credit_period_days_str, due_date_dd_mm_yyyy)`` — both
    normalized regardless of which form Tally exported.

    The detail sheet's ``Credit Period`` column always renders as a day count
    (e.g. ``"37 Days"``), and ``Due Date`` always renders as DD/MM/YYYY. If
    Tally only had a day count, we add it to the voucher date for Due Date;
    if Tally had a date, we back-compute the days. Both blank if input is
    blank or unparseable.
    """
    if not credit_period:
        return "", ""
    voucher_date: "datetime.date | None" = None
    if voucher_date_yyyymmdd and len(voucher_date_yyyymmdd) >= 8:
        try:
            voucher_date = datetime.strptime(voucher_date_yyyymmdd, "%Y%m%d").date()
        except ValueError:
            voucher_date = None
    parsed = _parse_credit_period(credit_period, voucher_date)
    if not parsed:
        return "", ""
    days, due = parsed
    days_str = f"{days} Days" if days >= 0 else ""
    return days_str, due.strftime("%d/%m/%Y")


_FX_NUMBER_RE = re.compile(r"[-+]?[\d,]+(?:\.\d+)?")


def _amount_abs(amount: str) -> str:
    """Return a clean, sign-stripped numeric amount from a Tally AMOUNT string.

    Two forms appear in the wild:

    * Plain numeric: ``"-1234.56"`` or ``"+1,234.56"`` → strip sign, return
      the number.
    * Foreign-currency expression: for export sales (and any voucher not in
      the home currency), Tally embeds the FX conversion directly in the
      AMOUNT string, e.g. ``"$9000.00 @ ₹90.80/$ = -₹817200.00"`` (the rupee
      symbols often arrive as ``?`` after our XML sanitization strips them).
      Take the home-currency amount after the last ``"="`` — that's what the
      receivables sheet actually wants in INR.

    Returns ``""`` for blank input or unparseable garbage (so data-quality
    issues stay visible rather than silently zeroed).
    """
    if not amount:
        return ""
    s = amount.strip()
    # FX expression — take the part after the last "="
    if "=" in s:
        s = s.rsplit("=", 1)[1]
    # Find the first numeric token (with optional sign and thousands commas)
    m = _FX_NUMBER_RE.search(s)
    if not m:
        return ""
    return m.group(0).lstrip("-").lstrip("+").strip()


def voucher_to_detail_rows(
    voucher: ET.Element,
    company: str,
    location: str,
    voucher_row: dict[str, Any],
) -> list[dict[str, Any]]:
    """Fan one voucher out into one row per real BILLALLOCATIONS.LIST entry.

    Bill allocations live under the **party's** LEDGERENTRIES.LIST leg (the
    debtor that owes money). Tax/discount legs typically don't carry
    allocations. We scan every ledger leg, collect every real allocation
    across them, then emit one row per allocation.

    Vouchers with zero real allocations emit a single synthetic ``On
    Account`` row carrying the voucher's gross total — guarantees
    ``SUM(Bill Amount)`` on the detail sheet equals ``SUM(Gross Total)`` on
    the per-voucher sheet voucher-for-voucher.

    ``voucher_row`` is the already-built per-voucher dict (so we can lift
    Date / Particulars / Voucher Type / Voucher No. / Month without
    re-deriving them).
    """
    date_raw = _text(voucher.find("DATE"))

    base: dict[str, Any] = {
        "company": company,
        "location": location,
        "month": voucher_row.get("month", ""),
        "date": voucher_row.get("date", ""),
        "particulars": voucher_row.get("particulars", ""),
        "voucher_type": voucher_row.get("voucher_type", ""),
        "voucher_no": voucher_row.get("voucher_no", ""),
        # Inherit the voucher's resolved GUID — bill allocations sit under the
        # party (debtor) leg, so the detail rows share the voucher's ledger_id.
        "ledger_id": voucher_row.get("ledger_id", ""),
    }

    allocations: list[ET.Element] = []
    for leg in list(voucher.findall("LEDGERENTRIES.LIST")) + list(voucher.findall("ALLLEDGERENTRIES.LIST")):
        for alloc in leg.findall("BILLALLOCATIONS.LIST"):
            if _is_real_allocation(alloc):
                allocations.append(alloc)

    if not allocations:
        # Synthetic On Account row — full gross total, blank credit/due.
        row = dict(base)
        row.update({
            "bill_ref_name": "",
            "bill_type": "On Account",
            "bill_amount": _amount_abs(voucher_row.get("gross_total", "")),
            "credit_period": "",
            "due_date": "",
        })
        return [row]

    # Pre-pass: count occurrences of each NAME so we can disambiguate
    # duplicates within a single voucher. Tally allows the same bill ref name
    # on multiple allocations of one voucher (e.g. two Agst Ref rows both
    # carrying the voucher's own number, or multiple equal-amount advance
    # splits). Without disambiguation, the (Company, Location, Voucher No.,
    # Bill Ref Name) dedupe key collapses them and we lose the duplicate
    # amounts on push. Suffix the 2nd, 3rd, ... occurrences with " (#N)" so
    # each row gets a unique key while the first occurrence keeps Tally's
    # exact name (the common case).
    name_counts: dict[str, int] = {}
    for a in allocations:
        n = _text(a.find("NAME"))
        name_counts[n] = name_counts.get(n, 0) + 1
    name_seen: dict[str, int] = {}

    rows: list[dict[str, Any]] = []
    for alloc in allocations:
        raw_name = _text(alloc.find("NAME"))
        if name_counts[raw_name] > 1:
            name_seen[raw_name] = name_seen.get(raw_name, 0) + 1
            bill_ref_name = raw_name if name_seen[raw_name] == 1 else f"{raw_name} (#{name_seen[raw_name]})"
        else:
            bill_ref_name = raw_name
        raw_period = _text(alloc.find("BILLCREDITPERIOD"))
        bill_type = _text(alloc.find("BILLTYPE"))
        # Tally exports BILLCREDITPERIOD as either "37 Days" or "17-Jul-25"
        # depending on what the user typed. Normalize to (days, due_date).
        # New Ref: creates a new bill — credit period is meaningful.
        # Advance with no credit period: due on the voucher date (0 days).
        # Agst Ref / On Account: no due date concept, leave blank.
        if bill_type.lower() == "new ref":
            if raw_period.strip():
                credit_days, due_date = resolve_credit_period_and_due_date(raw_period, date_raw)
            else:
                # Blank credit period on New Ref → treat as due on voucher date.
                credit_days = "0 Days"
                due_date = format_date(date_raw)
        elif bill_type.lower() == "advance":
            if raw_period.strip():
                credit_days, due_date = resolve_credit_period_and_due_date(raw_period, date_raw)
            else:
                # Advance with no credit period → due on the voucher date itself.
                credit_days = "0 Days"
                due_date = format_date(date_raw)
        else:
            credit_days, due_date = "", ""
        row = dict(base)
        row.update({
            "bill_ref_name": bill_ref_name,
            "bill_type": bill_type,
            "bill_amount": _amount_abs(_text(alloc.find("AMOUNT"))),
            "credit_period": credit_days,
            "due_date": due_date,
        })
        rows.append(row)
    return rows


def voucher_to_row(
    voucher: ET.Element,
    company: str,
    location: str,
    guid_map: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Build one aggregated row per voucher (one item line, summed values)."""
    date_raw = _text(voucher.find("DATE"))
    voucher_type_name = _text(voucher.find("VOUCHERTYPENAME"))
    voucher_no = _text(voucher.find("VOUCHERNUMBER"))
    party = _text(voucher.find("PARTYLEDGERNAME")) or _text(voucher.find("PARTYNAME"))
    gstin = _text(voucher.find("PARTYGSTIN"))
    gross_total = _voucher_gross_total(voucher, party)

    row: dict[str, Any] = {
        "company": company,
        "location": location,
        "month": derive_month(date_raw),
        "type": derive_type(voucher_type_name),
        "date": format_date(date_raw),
        "particulars": party,
        "voucher_type": voucher_type_name,
        "voucher_no": voucher_no,
        "gstin": gstin,
        "gross_total": gross_total,
    }
    row.update(_aggregate_inventory(voucher))
    # Resolve the party name → GUID (ledger_id). Display-only name (particulars)
    # stays on the row; identity will key on this GUID (LEDGER_ID_MIGRATION.md).
    # The detail rows inherit this same ledger_id (carried via voucher_row) so
    # the two sheets agree on the GUID for a shared (Company, Location, Voucher).
    guid_map = guid_map or {}
    row["ledger_id"] = guid_map.get(party.strip().upper(), "")
    return row


def project_columns(rows: Iterable[dict[str, Any]], columns: list[Column]) -> list[dict[str, Any]]:
    """Drop any keys not in the schema and ensure every schema key is present."""
    keys = [c.key for c in columns]
    out: list[dict[str, Any]] = []
    for r in rows:
        d = {k: r.get(k, "") for k in keys}
        # Carry the GUID as an extra field even though it is not (yet) a column
        # in reference/columns.md (or columns_details.md). The push tool only
        # writes columns.md columns, so this is invisible to the sheet / daily
        # sync until ledger_id is promoted to a real column at the migration gate.
        if "ledger_id" in r:
            d["ledger_id"] = r.get("ledger_id", "")
        out.append(d)
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="Fetch Tally sales vouchers as JSON.")
    parser.add_argument("--from", dest="from_date", required=True, help="Start date DD-MM-YYYY")
    parser.add_argument("--to", dest="to_date", required=True, help="End date DD-MM-YYYY")
    parser.add_argument("--companies", default="", help="Comma-separated subset of loaded companies (default: all)")
    parser.add_argument("--output", required=True, help="Output JSON path (e.g. .tmp/sales_<ts>.json)")
    args = parser.parse_args()

    load_dotenv()
    host = os.environ.get("TALLY_HOST", "http://localhost:9000").rstrip("/")

    from_date = parse_dmy(args.from_date)
    to_date = parse_dmy(args.to_date)

    columns = load_columns()
    detail_columns = load_details_columns()
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
    voucher_rows: list[dict[str, Any]] = []
    detail_rows: list[dict[str, Any]] = []
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
            vrow = voucher_to_row(v, company=display_company, location=location, guid_map=guid_map)
            voucher_rows.append(vrow)
            detail_rows.extend(voucher_to_detail_rows(v, display_company, location, vrow))

    vouchers_out = project_columns(voucher_rows, columns)
    details_out = project_columns(detail_rows, detail_columns)

    # ledger_id resolution stats (D5 visibility): how many party rows resolved
    # to a Tally GUID vs not (a blank GUID = mis-booking or missing master).
    # Computed on the per-voucher rows (one party each); the detail rows
    # inherit the same GUID, so the voucher-level rate is representative.
    named = [r for r in voucher_rows if (r.get("particulars") or "").strip()]
    resolved = sum(1 for r in named if (r.get("ledger_id") or "").strip())
    unresolved = len(named) - resolved
    unresolved_names = sorted({
        r["particulars"].strip()
        for r in named
        if not (r.get("ledger_id") or "").strip()
    })

    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    bundle = {"vouchers": vouchers_out, "details": details_out}
    out_path.write_text(json.dumps(bundle, indent=2, ensure_ascii=False), encoding="utf-8")

    summary = {
        "vouchers": voucher_count,
        "rows": len(vouchers_out),
        "detail_rows": len(details_out),
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
