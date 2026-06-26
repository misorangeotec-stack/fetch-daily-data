"""Fetch the full stock-item master from Tally Prime via XML over HTTP.

For each loaded company (or a user-supplied subset):
  1. Pull every Stock Item with NAME, GUID, PARENT (stock group),
     CATEGORY (separate stock-category dim), BASEUNITS, ADDITIONALUNITS,
     PARTNO, OPENINGBALANCE, OPENINGVALUE, STANDARDCOST, STANDARDPRICE,
     HSNCODE, REORDERLEVEL, MINIMUMORDERQTY, REORDERQTY, GSTDETAILS.LIST
     and LANGUAGENAME.LIST aliases. Tally evaluates OPENINGBALANCE /
     OPENINGVALUE against SVFROMDATE/SVTODATE — we pin those to a chosen
     FY so openings are deterministic regardless of which period the user
     has open in Tally's UI.
  2. Pull the Unit master once per company so we can resolve the
     conversion factor for items that use a compound (alternate) unit.
  3. Resolve company / location from reference/companies.md.

Sign convention:
  * `opening_value` is sign-flipped (Tally exports stock asset values as
    negative — same sign convention as ledger debit balances; we flip so
    the sheet reads positive for items in stock).
  * `opening_qty`, `standard_cost`, `standard_selling_price`,
    `reorder_level`, `reorder_quantity` are written as-is (numeric prefix
    only; unit suffix stripped).

Required env vars (loaded from project .env):
    TALLY_HOST   e.g. http://localhost:9000

Usage:
    python fetch_tally_stock_item_master.py [--from DD-MM-YYYY] [--to DD-MM-YYYY] \\
        [--companies "Co1,Co2"] --output .tmp/stock_item_master_<ts>.json
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _schema import Column, load_columns, load_companies  # noqa: E402

from list_tally_companies import list_companies  # noqa: E402


# Default window — FY 25-26. Used only when no --from / --to is supplied.
# Tally computes OPENINGBALANCE / OPENINGVALUE as the FY-start balance of
# the FY containing SVFROMDATE; the upper bound (SVTODATE) is mostly a
# correctness anchor for the same period. Master fields (item name, GST
# rate, unit, etc.) are date-independent.
FY_FROM_DATE = "20250401"
FY_TO_DATE = "20260331"


def _dmy_to_yyyymmdd(s: str) -> str:
    """Convert DD-MM-YYYY → YYYYMMDD for Tally's SVFROMDATE/SVTODATE."""
    from datetime import datetime
    return datetime.strptime(s.strip(), "%d-%m-%Y").strftime("%Y%m%d")


def _esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# Tally's XML output is messy: it mixes invalid UTF-8 bytes, raw control bytes
# (0x00-0x1F), and numeric char refs like &#0; that all break stdlib
# ElementTree. Decode tolerantly, strip XML-illegal chars, then re-encode.
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


def _t(el: ET.Element | None) -> str:
    if el is None or el.text is None:
        return ""
    return el.text.strip()


def fetch_units(host: str, company: str) -> dict[str, dict[str, str]]:
    """Return {unit_name: {"base": <name>, "additional": <name>, "conversion": <str>, "is_simple": "Yes"|"No"}}.

    Tally compound units carry a CONVERSION numeric (e.g. base PCS,
    additional NOS, conversion = 12 → 1 PCS = 12 NOS). Simple units have
    no compound parts and no conversion.
    """
    body = f"""<ENVELOPE>
  <HEADER><VERSION>1</VERSION><TALLYREQUEST>Export</TALLYREQUEST><TYPE>Collection</TYPE><ID>AllUnits</ID></HEADER>
  <BODY><DESC>
    <STATICVARIABLES>
      <SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
      <SVCURRENTCOMPANY>{_esc(company)}</SVCURRENTCOMPANY>
    </STATICVARIABLES>
    <TDL><TDLMESSAGE>
      <COLLECTION NAME="AllUnits" ISMODIFY="No"><TYPE>Unit</TYPE>
        <FETCH>NAME,BASEUNITS,ADDITIONALUNITS,CONVERSION,ISSIMPLEUNIT</FETCH>
      </COLLECTION>
    </TDLMESSAGE></TDL>
  </DESC></BODY></ENVELOPE>"""
    r = requests.post(host, data=body.encode("utf-8"), timeout=120)
    if r.status_code != 200:
        raise SystemExit(f"ERROR fetching units for '{company}': HTTP {r.status_code}")
    cleaned = _sanitize_tally_xml(r.content)
    try:
        root = ET.fromstring(cleaned)
    except ET.ParseError as exc:
        raise SystemExit(f"ERROR parsing units XML for '{company}': {exc}")
    out: dict[str, dict[str, str]] = {}
    for u in root.iter("UNIT"):
        nm = (u.attrib.get("NAME") or _t(u.find("NAME"))).strip()
        if not nm:
            continue
        out[nm] = {
            "base": _t(u.find("BASEUNITS")),
            "additional": _t(u.find("ADDITIONALUNITS")),
            "conversion": _t(u.find("CONVERSION")),
            "is_simple": _t(u.find("ISSIMPLEUNIT")),
        }
    return out


def fetch_stock_items(host: str, company: str, from_date: str, to_date: str) -> list[ET.Element]:
    """Return the STOCKITEM XML elements for every stock item in the company.

    `from_date` / `to_date` are YYYYMMDD strings driving SVFROMDATE / SVTODATE.
    Tally evaluates OPENINGBALANCE / OPENINGVALUE against the FY containing
    SVFROMDATE — pinning these makes openings deterministic regardless of
    which period the user has open in Tally's UI.
    """
    body = f"""<ENVELOPE>
  <HEADER><VERSION>1</VERSION><TALLYREQUEST>Export</TALLYREQUEST><TYPE>Collection</TYPE><ID>AllStockItems</ID></HEADER>
  <BODY><DESC>
    <STATICVARIABLES>
      <SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
      <SVCURRENTCOMPANY>{_esc(company)}</SVCURRENTCOMPANY>
      <SVFROMDATE TYPE="Date">{from_date}</SVFROMDATE>
      <SVTODATE TYPE="Date">{to_date}</SVTODATE>
    </STATICVARIABLES>
    <TDL><TDLMESSAGE>
      <COLLECTION NAME="AllStockItems" ISMODIFY="No"><TYPE>StockItem</TYPE>
        <FETCH>NAME,GUID,PARENT,CATEGORY,BASEUNITS,ADDITIONALUNITS,PARTNO,OPENINGBALANCE,OPENINGVALUE,STANDARDCOST,STANDARDPRICE,HSNCODE,GSTAPPLICABLE,REORDERLEVEL,MINIMUMORDERQTY,REORDERQTY,GSTDETAILS.LIST,LANGUAGENAME.LIST</FETCH>
      </COLLECTION>
    </TDLMESSAGE></TDL>
  </DESC></BODY></ENVELOPE>"""
    r = requests.post(host, data=body.encode("utf-8"), timeout=240)
    if r.status_code != 200:
        raise SystemExit(f"ERROR fetching stock items for '{company}': HTTP {r.status_code}")
    cleaned = _sanitize_tally_xml(r.content)
    try:
        root = ET.fromstring(cleaned)
    except ET.ParseError as exc:
        raise SystemExit(f"ERROR parsing stock-item XML for '{company}': {exc}")
    return list(root.iter("STOCKITEM"))


def _flip_signed(raw: str) -> str:
    """Tally → display: negate. Returns "" for blank / non-numeric.

    Tally exports stock asset values (OPENINGVALUE) as negative for items
    with a positive on-hand balance — same convention as ledger debit
    balances. We flip the sign so the sheet reads positive.
    """
    s = (raw or "").strip()
    if not s:
        return ""
    try:
        v = float(s.replace(",", ""))
    except ValueError:
        return ""
    flipped = -v
    if flipped == 0:
        flipped = 0.0
    return f"{flipped:.2f}"


_LEADING_NUM_RE = re.compile(r"-?\d+(?:\.\d+)?")


def _numeric_prefix(raw: str) -> str:
    """Extract the leading number from a Tally quantity/price string.

    Tally quantities look like "10 PCS", "12.5 KG", or "10 PCS = 120 NOS"
    (compound unit). We return only the base-unit numeric prefix as a
    plain string. Returns "" if no number is found.
    """
    if not raw:
        return ""
    m = _LEADING_NUM_RE.search(raw)
    return m.group(0) if m else ""


def _extract_alias(item: ET.Element, item_name: str) -> str:
    """Return the first alias from <LANGUAGENAME.LIST>.

    Same structure as ledgers — first NAME inside NAME.LIST is the primary
    item name; subsequent NAMEs are aliases.
    """
    for lang in item.iter("LANGUAGENAME.LIST"):
        for name_list in lang.findall("NAME.LIST"):
            names = [_t(n) for n in name_list.findall("NAME") if _t(n)]
            for nm in names[1:]:
                if nm and nm != item_name:
                    return nm
    return ""


def _extract_hsn(item: ET.Element) -> str:
    """First HSN code found across GSTDETAILS.LIST entries; fall back to
    top-level HSNCODE / HSN if Tally exposes it directly on the item.
    """
    for gst in item.iter("GSTDETAILS.LIST"):
        hsn = _t(gst.find("HSNCODE"))
        if hsn:
            return hsn
    # Some Tally configurations carry HSN at the item level.
    for tag in ("HSNCODE", "HSN"):
        v = _t(item.find(tag))
        if v:
            return v
    return ""


def _extract_gst_rate(item: ET.Element) -> str:
    """First IGST rate found across GSTDETAILS.LIST → STATEWISEDETAILS.LIST
    → RATEDETAILS.LIST. Returned as a plain numeric string (e.g. "18").
    """
    for gst in item.iter("GSTDETAILS.LIST"):
        for rd in gst.iter("RATEDETAILS.LIST"):
            head = _t(rd.find("GSTRATEDUTYHEAD")).upper()
            if head == "IGST":
                rate = _t(rd.find("GSTRATE"))
                if rate:
                    return _numeric_prefix(rate)
    return ""


def _conversion_for_base(base_unit: str, units: dict[str, dict[str, str]]) -> str:
    """Look up the conversion factor for the unit named `base_unit`.

    Tally stores compound units as a Unit record where ISSIMPLEUNIT=No,
    BASEUNITS=primary, ADDITIONALUNITS=secondary, CONVERSION=numeric.
    For a simple unit (or unknown), returns "".
    """
    if not base_unit:
        return ""
    info = units.get(base_unit)
    if not info:
        return ""
    if info.get("is_simple", "").lower() == "yes":
        return ""
    conv = info.get("conversion") or ""
    return _numeric_prefix(conv)


def stock_item_to_row(
    item: ET.Element,
    company: str,
    location: str,
    units: dict[str, dict[str, str]],
) -> dict[str, Any]:
    name = (item.attrib.get("NAME") or _t(item.find("NAME"))).strip()
    base_unit = _t(item.find("BASEUNITS"))
    return {
        "company": company,
        "location": location,
        "item_id": _t(item.find("GUID")),
        "item_name": name,
        "alias_name": _extract_alias(item, name),
        "sku_code": _t(item.find("PARTNO")),
        "category": _t(item.find("CATEGORY")),
        "sub_category": _t(item.find("PARENT")),
        "unit": base_unit,
        "alternate_unit": _t(item.find("ADDITIONALUNITS")),
        "conversion_factor": _conversion_for_base(base_unit, units),
        "opening_qty": _numeric_prefix(_t(item.find("OPENINGBALANCE"))),
        "opening_value": _flip_signed(_t(item.find("OPENINGVALUE"))),
        "standard_cost": _numeric_prefix(_t(item.find("STANDARDCOST"))),
        "standard_selling_price": _numeric_prefix(_t(item.find("STANDARDPRICE"))),
        "hsn_code": _extract_hsn(item),
        "gst_rate": _extract_gst_rate(item),
        "reorder_level": _numeric_prefix(_t(item.find("REORDERLEVEL"))),
        "reorder_quantity": (
            _numeric_prefix(_t(item.find("MINIMUMORDERQTY")))
            or _numeric_prefix(_t(item.find("REORDERQTY")))
        ),
        "is_active": "TRUE",
        # created_at / updated_at are filled in by the push tool at upsert time.
        "created_at": "",
        "updated_at": "",
    }


def project_columns(rows: list[dict[str, Any]], columns: list[Column]) -> list[dict[str, Any]]:
    keys = [c.key for c in columns]
    return [{k: r.get(k, "") for k in keys} for r in rows]


def main() -> int:
    parser = argparse.ArgumentParser(description="Fetch Tally stock-item master as JSON.")
    parser.add_argument("--companies", default="", help="Comma-separated subset of loaded companies (default: all)")
    parser.add_argument("--output", required=True, help="Output JSON path (e.g. .tmp/stock_item_master_<ts>.json)")
    parser.add_argument("--from", dest="from_dmy", default="", help="Period start, DD-MM-YYYY. Drives Tally SVFROMDATE; opening_qty / opening_value are computed at the FY-start of the FY containing this date. Default: 01-04-2025 (FY 25-26).")
    parser.add_argument("--to", dest="to_dmy", default="", help="Period end, DD-MM-YYYY. Drives Tally SVTODATE. Default: 31-03-2026 (FY 25-26).")
    args = parser.parse_args()

    from_date = _dmy_to_yyyymmdd(args.from_dmy) if args.from_dmy else FY_FROM_DATE
    to_date = _dmy_to_yyyymmdd(args.to_dmy) if args.to_dmy else FY_TO_DATE

    load_dotenv()
    host = os.environ.get("TALLY_HOST", "http://localhost:9000").rstrip("/")

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

    warned_company: set[str] = set()
    all_rows: list[dict[str, Any]] = []
    per_company_counts: dict[str, int] = {}
    no_guid = 0

    for raw_company in target_companies:
        if raw_company in company_map:
            display_company, location = company_map[raw_company]
        else:
            if raw_company not in warned_company:
                print(
                    f"WARNING: '{raw_company}' is not in reference/companies.md — "
                    "using raw name and blank location. Add a row to the mapping "
                    "to fix this.",
                    file=sys.stderr,
                )
                warned_company.add(raw_company)
            display_company, location = raw_company, ""

        units = fetch_units(host, raw_company)
        items = fetch_stock_items(host, raw_company, from_date, to_date)
        kept = 0
        for it in items:
            row = stock_item_to_row(it, display_company, location, units)
            if not row["item_name"]:
                continue
            if not row["item_id"]:
                no_guid += 1
            all_rows.append(row)
            kept += 1
        per_company_counts[f"{display_company} / {location}"] = kept

    if no_guid:
        print(
            f"WARNING: {no_guid} stock item(s) had no GUID — these will collide "
            "on upsert if their (company, location, '') key is non-unique. "
            "Investigate.",
            file=sys.stderr,
        )

    rows = project_columns(all_rows, columns)

    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(rows, indent=2, ensure_ascii=False), encoding="utf-8")

    summary = {
        "rows": len(rows),
        "per_company": per_company_counts,
        "no_guid": no_guid,
        "output": str(out_path),
    }
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
