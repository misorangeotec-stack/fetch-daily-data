"""Fetch the full ledger master from Tally Prime via XML over HTTP.

For each loaded company (or a user-supplied subset):
  1. Pull every Group with NAME, PARENT and walk the chain to find each
     ledger's top-level ancestor (used for primary_group / ledger_type
     classification).
  2. Pull every Ledger with NAME, GUID, PARENT, OPENINGBALANCE,
     CLOSINGBALANCE, BILLCREDITPERIOD, CREDITLIMIT, contact / GSTIN /
     address fields, and the LANGUAGENAME aliases for the requested date
     window. Tally computes OPENINGBALANCE as the FY-start balance of the
     FY containing SVFROMDATE, and CLOSINGBALANCE as of SVTODATE.
  3. For each ledger, derive `primary_group` and `ledger_type` by walking
     the parent chain to the top and checking against `CLASSIFICATION_RULES`.
  4. Resolve company / location from reference/companies.md.

Sign convention:
  * `opening_balance`  → absolute value, with `opening_balance_type` = Dr/Cr
    (Tally exports debits as negative, so raw < 0 → Dr).
  * `closing_balance`  → sign-flipped (positive = Dr, negative = Cr).
  * `credit_limit`     → sign-flipped (Tally stores it negative for debtor
    ledgers; the sheet shows positive).

Required env vars (loaded from project .env):
    TALLY_HOST   e.g. http://localhost:9000

Usage:
    python fetch_tally_ledger_master.py [--companies "Co1,Co2"] \\
        --output .tmp/ledger_master_<ts>.json
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


# Default window — FY 25-26. Used only when no --from / --to / --from-date /
# --to-date is supplied. Tally computes OPENINGBALANCE as the FY-start
# balance of the FY containing SVFROMDATE, and CLOSINGBALANCE as of SVTODATE.
FY_FROM_DATE = "20250401"
FY_TO_DATE = "20260331"


def _dmy_to_yyyymmdd(s: str) -> str:
    """Convert DD-MM-YYYY → YYYYMMDD for Tally's SVFROMDATE/SVTODATE."""
    from datetime import datetime
    return datetime.strptime(s.strip(), "%d-%m-%Y").strftime("%Y%m%d")


# Walk-to-top classification of a ledger's parent chain.
# First match wins. Each rule: (set of root-group names to look for in the
# chain, primary_group label, ledger_type label).
CLASSIFICATION_RULES: list[tuple[set[str], str, str]] = [
    ({"Sundry Debtors"},                                          "Debtors",   "Customer"),
    ({"Sundry Creditors"},                                         "Creditors", "Supplier"),
    ({"Bank Accounts", "Bank OD A/c"},                             "Bank",      "Bank"),
    ({"Direct Expenses", "Indirect Expenses", "Purchase Accounts"}, "Expense",   "Expense"),
    ({"Direct Incomes", "Indirect Incomes", "Sales Accounts"},     "Income",    "Income"),
]


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


def build_parent_map(groups: list[tuple[str, str]]) -> dict[str, str]:
    """name → direct parent name (top-level groups have parent ""). """
    return {n: p for n, p in groups}


def parent_chain(name: str, parent_map: dict[str, str]) -> list[str]:
    """Return the chain from `name` up to its top-level ancestor (exclusive
    of the empty parent), [name, ..., top]. Stops on cycle or empty parent.
    """
    chain: list[str] = []
    seen: set[str] = set()
    current = name
    while current and current not in seen:
        chain.append(current)
        seen.add(current)
        current = parent_map.get(current, "")
    return chain


def classify(chain: list[str]) -> tuple[str, str, str]:
    """Return (primary_group, ledger_type, top_level_name).

    - top_level_name is the topmost *real* group in the chain — i.e. the
      last item, but skipping Tally's "Primary" root pseudo-group, which
      sits above every user-visible top-level group and is structurally
      meaningless for classification.
    - If the chain hits any of the well-known classification roots, that
      rule wins (first-match precedence per CLASSIFICATION_RULES).
    """
    if not chain:
        return "", "", ""
    chain_set = set(chain)
    for roots, primary, lt in CLASSIFICATION_RULES:
        if chain_set & roots:
            return primary, lt, chain[-1]
    # Fallback: walk from the top down, skipping "Primary" so the label is
    # a meaningful top-level group like "Capital Account" or "Duties &
    # Taxes" rather than Tally's root pseudo-group.
    for ancestor in reversed(chain):
        if ancestor and ancestor != "Primary":
            return ancestor, "", ancestor
    return "", "", ""


def fetch_ledgers(host: str, company: str, from_date: str, to_date: str) -> list[ET.Element]:
    """Return the LEDGER XML elements for every ledger in the company."""
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
        <FETCH>NAME,GUID,PARENT,OPENINGBALANCE,CLOSINGBALANCE,BILLCREDITPERIOD,CREDITLIMIT,PARTYGSTIN,INCOMETAXNUMBER,LEDSTATENAME,COUNTRYOFRESIDENCE,PINCODE,LEDGERCONTACT,LEDGERPHONE,LEDGERMOBILE,EMAIL,LANGUAGENAME.LIST</FETCH>
      </COLLECTION>
    </TDLMESSAGE></TDL>
  </DESC></BODY></ENVELOPE>"""
    r = requests.post(host, data=body.encode("utf-8"), timeout=240)
    if r.status_code != 200:
        raise SystemExit(f"ERROR fetching ledgers for '{company}': HTTP {r.status_code}")
    cleaned = _sanitize_tally_xml(r.content)
    try:
        root = ET.fromstring(cleaned)
    except ET.ParseError as exc:
        raise SystemExit(f"ERROR parsing ledger XML for '{company}': {exc}")
    return list(root.iter("LEDGER"))


def _flip_signed(raw: str) -> str:
    """Tally → display: negate. Returns "" for blank / non-numeric.

    Used for `closing_balance` and `credit_limit` where we keep the sign so
    callers can tell Dr (positive) from Cr (negative).
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


def _abs_with_dr_cr(raw: str) -> tuple[str, str]:
    """Split a Tally-signed balance into (abs_value_str, type).

    Tally exports debit balances as negative, so:
      raw < 0  → Dr (debit)
      raw > 0  → Cr (credit)
      raw == 0 → ""

    Returns ("", "") for blank / non-numeric input.
    """
    s = (raw or "").strip()
    if not s:
        return "", ""
    try:
        v = float(s.replace(",", ""))
    except ValueError:
        return "", ""
    if v == 0:
        return "0.00", ""
    if v < 0:
        return f"{-v:.2f}", "Dr"
    return f"{v:.2f}", "Cr"


_CREDIT_PERIOD_RE = re.compile(r"(\d+)")


def _parse_credit_period_days(raw: str) -> str:
    """Parse Tally's BILLCREDITPERIOD ("45 Days", "60 Days", "") to int days
    as a string, or "" if unparseable.
    """
    if not raw:
        return ""
    m = _CREDIT_PERIOD_RE.search(raw)
    return m.group(1) if m else ""


def _extract_alias(led: ET.Element, ledger_name: str) -> str:
    """Return the first alias from <LANGUAGENAME.LIST>.

    Tally's structure for each entry:
        <LANGUAGENAME.LIST>
          <NAME.LIST TYPE="String">
            <NAME>Primary Name</NAME>
            <NAME>Alias 1</NAME>
            ...
          </NAME.LIST>
          <LANGUAGEID>1033</LANGUAGEID>
        </LANGUAGENAME.LIST>

    The first NAME inside NAME.LIST is the primary ledger name; subsequent
    NAMEs are aliases. We return the first alias found across all language
    entries (typically there's one English entry).
    """
    for lang in led.iter("LANGUAGENAME.LIST"):
        for name_list in lang.findall("NAME.LIST"):
            names = [_t(n) for n in name_list.findall("NAME") if _t(n)]
            # First entry is the primary name; anything after it is an alias.
            for nm in names[1:]:
                if nm and nm != ledger_name:
                    return nm
    return ""


def ledger_to_row(
    led: ET.Element,
    company: str,
    location: str,
    parent_map: dict[str, str],
) -> dict[str, Any]:
    name = (led.attrib.get("NAME") or _t(led.find("NAME"))).strip()
    parent = _t(led.find("PARENT"))

    chain = parent_chain(parent, parent_map) if parent else []
    primary_group, ledger_type, _top = classify(chain)

    opening_raw = _t(led.find("OPENINGBALANCE"))
    opening_abs, opening_type = _abs_with_dr_cr(opening_raw)

    phone = _t(led.find("LEDGERPHONE")) or _t(led.find("LEDGERMOBILE"))

    return {
        "company": company,
        "location": location,
        "ledger_id": _t(led.find("GUID")),
        "ledger_name": name,
        "alias_name": _extract_alias(led, name),
        "parent_group": parent,
        "primary_group": primary_group,
        "ledger_type": ledger_type,
        "opening_balance": opening_abs,
        "opening_balance_type": opening_type,
        "closing_balance": _flip_signed(_t(led.find("CLOSINGBALANCE"))),
        "credit_limit": _flip_signed(_t(led.find("CREDITLIMIT"))),
        "credit_period_days": _parse_credit_period_days(_t(led.find("BILLCREDITPERIOD"))),
        "gstin": _t(led.find("PARTYGSTIN")),
        "pan_number": _t(led.find("INCOMETAXNUMBER")),
        "state": _t(led.find("LEDSTATENAME")),
        "country": _t(led.find("COUNTRYOFRESIDENCE")),
        "pincode": _t(led.find("PINCODE")),
        "contact_person": _t(led.find("LEDGERCONTACT")),
        "phone": phone,
        "email": _t(led.find("EMAIL")),
        "is_active": "TRUE",
        # created_at / updated_at are filled in by the push tool at upsert time.
        "created_at": "",
        "updated_at": "",
    }


def project_columns(rows: list[dict[str, Any]], columns: list[Column]) -> list[dict[str, Any]]:
    keys = [c.key for c in columns]
    return [{k: r.get(k, "") for k in keys} for r in rows]


def main() -> int:
    parser = argparse.ArgumentParser(description="Fetch Tally ledger master as JSON.")
    parser.add_argument("--companies", default="", help="Comma-separated subset of loaded companies (default: all)")
    parser.add_argument("--output", required=True, help="Output JSON path (e.g. .tmp/ledger_master_<ts>.json)")
    # Preferred date args, matching sibling Tally sync skills (DD-MM-YYYY).
    parser.add_argument("--from", dest="from_dmy", default="", help="Period start, DD-MM-YYYY. Drives Tally SVFROMDATE; OPENINGBALANCE is computed at the FY-start of the FY containing this date.")
    parser.add_argument("--to", dest="to_dmy", default="", help="Period end, DD-MM-YYYY. Drives Tally SVTODATE; CLOSINGBALANCE is computed as of this date.")
    # Back-compat: raw YYYYMMDD args. Used only if --from / --to are not provided.
    parser.add_argument("--from-date", default=FY_FROM_DATE, help=f"(legacy) Period start, YYYYMMDD. Default: {FY_FROM_DATE}")
    parser.add_argument("--to-date", default=FY_TO_DATE, help=f"(legacy) Period end, YYYYMMDD. Default: {FY_TO_DATE}")
    args = parser.parse_args()

    if args.from_dmy:
        args.from_date = _dmy_to_yyyymmdd(args.from_dmy)
    if args.to_dmy:
        args.to_date = _dmy_to_yyyymmdd(args.to_dmy)

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
    no_classification = 0

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

        groups = fetch_groups(host, raw_company)
        parent_map = build_parent_map(groups)

        ledgers = fetch_ledgers(host, raw_company, args.from_date, args.to_date)
        kept = 0
        for led in ledgers:
            row = ledger_to_row(led, display_company, location, parent_map)
            if not row["ledger_name"]:
                continue
            if not row["ledger_id"]:
                no_guid += 1
            if not row["primary_group"]:
                no_classification += 1
            all_rows.append(row)
            kept += 1
        per_company_counts[f"{display_company} / {location}"] = kept

    if no_guid:
        print(
            f"WARNING: {no_guid} ledger(s) had no GUID — these will collide on "
            "upsert if their (company, location, '') key is non-unique. Investigate.",
            file=sys.stderr,
        )
    if no_classification:
        print(
            f"NOTE: {no_classification} ledger(s) had no parent / no classifiable "
            "top-level group; primary_group / ledger_type left blank.",
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
        "no_classification": no_classification,
        "output": str(out_path),
    }
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
