"""Fetch Sundry Debtor ledger master data (Credit Period, Credit Limit,
Opening Apr-25, Opening Apr-26) from Tally Prime via XML over HTTP.

For each loaded company (or a user-supplied subset):
  1. Pull every Group with NAME, PARENT and walk the parent chain to find all
     groups whose ancestor is "Sundry Debtors" — call this the SD-set.
  2. Pull every Ledger with NAME, PARENT, OPENINGBALANCE, CLOSINGBALANCE,
     BILLCREDITPERIOD, CREDITLIMIT for the FY 25-26 date window
     (1-Apr-2025 → 31-Mar-2026). Tally computes CLOSINGBALANCE based on the
     SVFROMDATE/SVTODATE static variables.
  3. Keep only ledgers whose PARENT is in the SD-set. Each kept ledger
     becomes one output row.
  4. Sign-flip OPENINGBALANCE, CLOSINGBALANCE, CREDITLIMIT so that a debit
     balance (a normal receivable) appears as a positive number in the sheet,
     matching the original references/Credit Limit & Opening.xlsx convention.
  5. Resolve company / location from reference/companies.md and Sales Person
     from reference/sales_persons.md (both human-editable).

Required env vars (loaded from project .env):
    TALLY_HOST   e.g. http://localhost:9000

Usage:
    python fetch_tally_credit_limits.py [--companies "Co1,Co2"] \\
        --output .tmp/credit_limits_<ts>.json
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
from _schema import Column, load_columns, load_companies, load_excluded_ledgers, load_opening_overrides, load_sales_persons  # noqa: E402

from list_tally_companies import list_companies  # noqa: E402


# FY 25-26 — fixed for now. When a separate FY 26-27 company is loaded in
# Tally, switch the workflow to read OPENINGBALANCE from that book for
# Apr-26 instead of CLOSINGBALANCE from this one.
FY_FROM_DATE = "20250401"
FY_TO_DATE = "20260331"

SUNDRY_DEBTORS_ROOT = "Sundry Debtors"


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


def fetch_groups(host: str, company: str) -> list[tuple[str, str]]:
    """Return [(group_name, parent_group_name), ...] for the company."""
    body = f"""<ENVELOPE>
  <HEADER><VERSION>1</VERSION><TALLYREQUEST>Export</TALLYREQUEST><TYPE>Collection</TYPE><ID>AllGroups</ID></HEADER>
  <BODY><DESC>
    <STATICVARIABLES>
      <SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
      <SVCURRENTCOMPANY>{_esc(company)}</SVCURRENTCOMPANY>
    </STATICVARIABLES>
    <TDL><TDLMESSAGE>
      <COLLECTION NAME="AllGroups" ISMODIFY="No"><TYPE>Group</TYPE><FETCH>NAME,PARENT</FETCH></COLLECTION>
    </TDLMESSAGE></TDL>
  </DESC></BODY></ENVELOPE>"""
    r = requests.post(host, data=body.encode("utf-8"), timeout=60)
    if r.status_code != 200:
        raise SystemExit(f"ERROR fetching groups for '{company}': HTTP {r.status_code}")
    cleaned = _sanitize_tally_xml(r.content)
    try:
        root = ET.fromstring(cleaned)
    except ET.ParseError as exc:
        raise SystemExit(f"ERROR parsing groups XML for '{company}': {exc}")
    out: list[tuple[str, str]] = []
    for g in root.iter("GROUP"):
        nm = (g.attrib.get("NAME") or _t(g.find("NAME"))).strip()
        parent = _t(g.find("PARENT"))
        if nm:
            out.append((nm, parent))
    return out


def descendants_of(groups: list[tuple[str, str]], root_name: str) -> set[str]:
    """Return the set of every group whose ancestor chain includes
    ``root_name`` (inclusive of the root itself).

    Tally lets users nest receivable groups arbitrarily deep (e.g.
    ``Sundry Debtors → Punjab → Amritsar``). Walking the chain — instead of
    only matching ``PARENT == 'Sundry Debtors'`` — picks up ledgers under
    every nested receivable group.
    """
    out: set[str] = {root_name}
    changed = True
    while changed:
        changed = False
        for n, p in groups:
            if n not in out and p in out:
                out.add(n)
                changed = True
    return out


def fetch_ledgers(host: str, company: str, from_date: str, to_date: str) -> list[ET.Element]:
    """Return the LEDGER XML elements for every ledger in the company.

    Filtering down to Sundry Debtors happens client-side (cheaper than
    crafting a TDL filter, and more reliable across Tally configurations).
    """
    body = f"""<ENVELOPE>
  <HEADER><VERSION>1</VERSION><TALLYREQUEST>Export</TALLYREQUEST><TYPE>Collection</TYPE><ID>AllLedgers</ID></HEADER>
  <BODY><DESC>
    <STATICVARIABLES>
      <SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
      <SVCURRENTCOMPANY>{_esc(company)}</SVCURRENTCOMPANY>
      <SVFROMDATE TYPE="Date">{from_date}</SVFROMDATE>
      <SVTODATE TYPE="Date">{to_date}</SVTODATE>
    </STATICVARIABLES>
    <TDL><TDLMESSAGE>
      <COLLECTION NAME="AllLedgers" ISMODIFY="No"><TYPE>Ledger</TYPE>
        <FETCH>NAME,PARENT,OPENINGBALANCE,CLOSINGBALANCE,BILLCREDITPERIOD,CREDITLIMIT,GUID</FETCH>
      </COLLECTION>
    </TDLMESSAGE></TDL>
  </DESC></BODY></ENVELOPE>"""
    r = requests.post(host, data=body.encode("utf-8"), timeout=180)
    if r.status_code != 200:
        raise SystemExit(f"ERROR fetching ledgers for '{company}': HTTP {r.status_code}")
    cleaned = _sanitize_tally_xml(r.content)
    try:
        root = ET.fromstring(cleaned)
    except ET.ParseError as exc:
        raise SystemExit(f"ERROR parsing ledger XML for '{company}': {exc}")
    return list(root.iter("LEDGER"))


def _abs_amount(raw: str) -> str:
    """Return the absolute value of a Tally amount string.

    Tally's XML sign convention is not consistent across companies (standard
    companies export Dr balances as negative; split/archived companies often
    invert this). Taking the absolute value gives a displayable positive number
    regardless of convention. Returns "" for empty / non-numeric input.
    """
    s = (raw or "").strip()
    if not s:
        return ""
    try:
        v = float(s.replace(",", ""))
    except ValueError:
        return ""
    return f"{abs(v):.2f}"


def _dr_cr(raw: str) -> str:
    """Return "Dr", "Cr", or "" based on the sign of a Tally amount string.

    Standard Tally convention: negative XML value = Dr (debit / receivable),
    positive = Cr (advance / credit balance). This holds for all four standard
    companies; split/archived companies with a date-range suffix in their name
    may invert this — flag them in reference/companies.md if needed.
    """
    s = (raw or "").strip()
    if not s:
        return ""
    try:
        v = float(s.replace(",", ""))
    except ValueError:
        return ""
    if v < 0:
        return "Dr"
    if v > 0:
        return "Cr"
    return ""


def ledger_to_row(led: ET.Element, company: str, location: str, sales_persons: dict[str, str], unmapped_sp: set[str], apr25_mode: str = "", opening_overrides: dict[tuple[str, str, str], tuple[str, str]] | None = None) -> dict[str, Any]:
    name = (led.attrib.get("NAME") or _t(led.find("NAME"))).strip()
    sp = sales_persons.get(name, "")
    if not sp:
        unmapped_sp.add(name)
    ob_raw = _t(led.find("OPENINGBALANCE"))
    cb_raw = _t(led.find("CLOSINGBALANCE"))
    # FY-rollover continuation books (apr25_opening=zero in companies.md): their
    # Tally OPENINGBALANCE is the carried-forward FY26 opening, not a true
    # 1-Apr-2025 opening — force Apr-25 to 0 so it isn't phantom-counted. Apr-26
    # (closing) is left intact. See companies.md drift note + load_companies().
    if apr25_mode == "zero":
        ob_raw = "0"
    # Per-ledger override (reference/opening_overrides.md) WINS over the blanket
    # zero: a specific ledger whose real 1-Apr-2025 opening can't be read from
    # the synced book (e.g. settled by FY26 so the continuation book reads 0).
    # The override carries an absolute amount + Dr/Cr, written directly below.
    override = (opening_overrides or {}).get((company, location, name.upper()))
    if override is not None:
        amt, drcr = override
        return {
            "company": company,
            "location": location,
            "name": name,
            "sales_person": sp,
            "credit_period": _t(led.find("BILLCREDITPERIOD")),
            "credit_limit": _abs_amount(_t(led.find("CREDITLIMIT"))),
            "opening_apr_25": _abs_amount(amt),
            "opening_apr_25_type": drcr,
            "opening_apr_26": _abs_amount(cb_raw),
            "opening_apr_26_type": _dr_cr(cb_raw),
            "ledger_id": _t(led.find("GUID")),
        }
    return {
        "company": company,
        "location": location,
        "name": name,
        "sales_person": sp,
        "credit_period": _t(led.find("BILLCREDITPERIOD")),
        "credit_limit": _abs_amount(_t(led.find("CREDITLIMIT"))),
        "opening_apr_25": _abs_amount(ob_raw),
        "opening_apr_25_type": _dr_cr(ob_raw),
        "opening_apr_26": _abs_amount(cb_raw),
        "opening_apr_26_type": _dr_cr(cb_raw),
        # The ledger's own Tally GUID — the stable identity key (survives renames).
        # This sheet IS the debtor master, so the GUID is read directly off the
        # ledger (no name→GUID map needed). See scripts/LEDGER_ID_MIGRATION.md (Phase D).
        "ledger_id": _t(led.find("GUID")),
    }


def project_columns(rows: list[dict[str, Any]], columns: list[Column]) -> list[dict[str, Any]]:
    keys = [c.key for c in columns]
    out: list[dict[str, Any]] = []
    for r in rows:
        d = {k: r.get(k, "") for k in keys}
        # Carry the GUID as an extra field even though it is not (yet) a column
        # in reference/columns.md. The push tool only writes columns.md columns,
        # so this is invisible to the sheet / daily sync until ledger_id is
        # promoted to a real column at the migration gate (Phase D).
        if "ledger_id" in r:
            d["ledger_id"] = r.get("ledger_id", "")
        out.append(d)
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="Fetch Tally Sundry Debtor master data as JSON.")
    parser.add_argument("--companies", default="", help="Comma-separated subset of loaded companies (default: all)")
    parser.add_argument("--output", required=True, help="Output JSON path (e.g. .tmp/credit_limits_<ts>.json)")
    parser.add_argument("--from-date", default=FY_FROM_DATE, help=f"FY start, YYYYMMDD (default: {FY_FROM_DATE})")
    parser.add_argument("--to-date", default=FY_TO_DATE, help=f"FY end, YYYYMMDD (default: {FY_TO_DATE})")
    args = parser.parse_args()

    load_dotenv()
    host = os.environ.get("TALLY_HOST", "http://localhost:9000").rstrip("/")

    columns = load_columns()
    company_map = load_companies()
    sales_persons = load_sales_persons()
    opening_overrides = load_opening_overrides()
    excluded_ledgers = load_excluded_ledgers()

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
    unmapped_sp: set[str] = set()
    all_rows: list[dict[str, Any]] = []
    per_company_counts: dict[str, int] = {}

    for raw_company in target_companies:
        if raw_company in company_map:
            display_company, location, apr25_mode = company_map[raw_company]
        else:
            if raw_company not in warned_company:
                print(
                    f"WARNING: '{raw_company}' is not in reference/companies.md — "
                    "using raw name and blank location. Add a row to the mapping "
                    "to fix this.",
                    file=sys.stderr,
                )
                warned_company.add(raw_company)
            display_company, location, apr25_mode = raw_company, "", ""

        groups = fetch_groups(host, raw_company)
        sd_set = descendants_of(groups, SUNDRY_DEBTORS_ROOT)

        ledgers = fetch_ledgers(host, raw_company, args.from_date, args.to_date)
        kept = 0
        for led in ledgers:
            parent = _t(led.find("PARENT"))
            if parent in sd_set:
                row = ledger_to_row(led, display_company, location, sales_persons, unmapped_sp, apr25_mode, opening_overrides)
                # Drop empty-name rows (defensive — shouldn't happen).
                if not row["name"]:
                    continue
                # Skip non-debtor ledgers that leak in under Sundry Debtors
                # (GL accruals / control accounts — see excluded_ledgers.md).
                if (display_company, location, row["name"].upper()) in excluded_ledgers:
                    continue
                all_rows.append(row)
                kept += 1
        per_company_counts[f"{display_company} / {location}"] = kept

    if unmapped_sp:
        sample = sorted(unmapped_sp)[:5]
        print(
            f"WARNING: {len(unmapped_sp)} ledger(s) have no Sales Person mapping in "
            f"reference/sales_persons.md (sample: {sample}). Sales Person column "
            "left blank for those rows. Add them to the mapping to fix.",
            file=sys.stderr,
        )

    rows = project_columns(all_rows, columns)

    # ledger_id is read directly off each ledger's GUID, so resolution should be
    # 100%; surface any blank (a ledger with no GUID = a Tally data anomaly).
    with_guid = sum(1 for r in all_rows if (r.get("ledger_id") or "").strip())
    without_guid = len(all_rows) - with_guid

    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(rows, indent=2, ensure_ascii=False), encoding="utf-8")

    summary = {
        "rows": len(rows),
        "per_company": per_company_counts,
        "unmapped_sales_persons": len(unmapped_sp),
        "ledger_id_resolved": with_guid,
        "ledger_id_blank": without_guid,
        "output": str(out_path),
    }
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
