"""Fetch the Profit & Loss statement (group level) from Tally Prime via XML.

For ONE user-chosen company:
  1. Pull the group structure (NAME, GUID, PARENT, ISREVENUE) — reliable.
  2. Pull Tally's **Trial Balance** report (the only reliable source of
     COMPUTED group closing balances on the HTTP/XML gateway — see the long
     note at the bottom of this file). With EXPLODEFLAG=Yes it returns each
     top-level group and its sub-groups with the Tally-computed net closing.
  3. Keep the rows that are groups (full group tree, classified into
     statement / side), filter to **Profit & Loss** groups, and write them.

Why not a Group/Ledger collection? A `<TYPE>Group</TYPE>` collection does not
roll up descendants (parents come back blank); a bulk `<TYPE>Ledger</TYPE>`
collection returns 0 for many forex/bill-wise ledgers. Both fail to
reconcile to Tally's real statement. The Trial Balance report is computed by
Tally itself and balances exactly (total Dr = total Cr).

Period model (single `--as-of DD-MM-YYYY`, default = today):
  * SVTODATE   = --as-of.
  * SVFROMDATE = 1-Apr of the FY containing --as-of (Indian FY), so each P&L
    group's closing is the income/expense for the period.

Sign convention (mirrors tally-ledger-master-sync):
  * `closing_balance` is sign-flipped — positive = Dr, negative = Cr. So
    Expense groups (debit) show positive and Income groups (credit) negative.

Required env vars (loaded from project .env):
    TALLY_HOST   e.g. http://localhost:9000

Usage:
    python fetch_tally_pnl.py --company "Co Name" \\
        [--as-of 15-06-2026] --output .tmp/pnl_<ts>.json
"""

from __future__ import annotations

import argparse
import datetime as _dt
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


# This skill keeps only rows belonging to this statement (the Balance Sheet
# skill is identical except STATEMENT = "Balance Sheet").
STATEMENT = "Profit & Loss"


# Statement classification, keyed on a group's TOP-LEVEL ancestor name.
# First match wins. These are Tally's standard primary groups; custom
# top-level groups fall through to the ISREVENUE + sign fallback below.
# Each rule: (set of top-level group names, statement, side).
STATEMENT_RULES: list[tuple[set[str], str, str]] = [
    ({"Capital Account", "Loans (Liability)", "Current Liabilities",
      "Suspense A/c", "Provisions"},                                 "Balance Sheet", "Liabilities"),
    ({"Fixed Assets", "Investments", "Current Assets",
      "Loans & Advances (Asset)", "Misc. Expenses (ASSET)",
      "Branch / Divisions"},                                         "Balance Sheet", "Assets"),
    ({"Sales Accounts", "Direct Incomes", "Indirect Incomes"},       "Profit & Loss", "Income"),
    ({"Purchase Accounts", "Direct Expenses", "Indirect Expenses"},  "Profit & Loss", "Expense"),
]


def _dmy_to_yyyymmdd(s: str) -> str:
    """Convert DD-MM-YYYY → YYYYMMDD for Tally's SVFROMDATE/SVTODATE."""
    return _dt.datetime.strptime(s.strip(), "%d-%m-%Y").strftime("%Y%m%d")


def fy_start_yyyymmdd(as_of_dmy: str) -> str:
    """Return the 1-Apr FY-start (YYYYMMDD) of the Indian FY containing
    `as_of_dmy` (DD-MM-YYYY). Apr–Mar year: month >= 4 → same year, else
    previous year."""
    d = _dt.datetime.strptime(as_of_dmy.strip(), "%d-%m-%Y")
    fy_year = d.year if d.month >= 4 else d.year - 1
    return f"{fy_year:04d}0401"


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


def _to_float(raw: str) -> float:
    s = (raw or "").strip().replace(",", "")
    if not s:
        return 0.0
    try:
        return float(s)
    except ValueError:
        return 0.0


def fetch_groups_meta(host: str, company: str) -> list[ET.Element]:
    """Return GROUP XML elements (structure only: NAME, GUID, PARENT,
    ISREVENUE). Group balances are taken from the Trial Balance, not here."""
    body = f"""<ENVELOPE>
  <HEADER><VERSION>1</VERSION><TALLYREQUEST>Export</TALLYREQUEST><TYPE>Collection</TYPE><ID>AllGroups</ID></HEADER>
  <BODY><DESC>
    <STATICVARIABLES>
      <SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
      <SVCURRENTCOMPANY>{_esc(company)}</SVCURRENTCOMPANY>
    </STATICVARIABLES>
    <TDL><TDLMESSAGE>
      <COLLECTION NAME="AllGroups" ISMODIFY="No"><TYPE>Group</TYPE>
        <FETCH>NAME,GUID,PARENT,ISREVENUE</FETCH>
      </COLLECTION>
    </TDLMESSAGE></TDL>
  </DESC></BODY></ENVELOPE>"""
    r = requests.post(host, data=body.encode("utf-8"), timeout=120)
    if r.status_code != 200:
        raise SystemExit(f"ERROR fetching groups for '{company}': HTTP {r.status_code}")
    try:
        root = ET.fromstring(_sanitize_tally_xml(r.content))
    except ET.ParseError as exc:
        raise SystemExit(f"ERROR parsing groups XML for '{company}': {exc}")
    return list(root.iter("GROUP"))


def fetch_trial_balance(host: str, company: str, from_date: str, to_date: str) -> list[tuple[str, float]]:
    """Return [(account_name, net_closing_raw), ...] from Tally's Trial
    Balance report (EXPLODEFLAG=Yes → top-level groups + their sub-groups).

    ``net_closing_raw`` is Tally's signed convention (debit negative): the
    sum of the row's DSPCLDRAMTA (debit, negative) and DSPCLCRAMTA (credit,
    positive). Rows preserve document order; both groups and ledgers appear
    (the caller keeps only the ones that are groups)."""
    body = f"""<ENVELOPE>
  <HEADER><TALLYREQUEST>Export Data</TALLYREQUEST></HEADER>
  <BODY><EXPORTDATA><REQUESTDESC>
    <REPORTNAME>Trial Balance</REPORTNAME>
    <STATICVARIABLES>
      <SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
      <SVCURRENTCOMPANY>{_esc(company)}</SVCURRENTCOMPANY>
      <SVFROMDATE TYPE="Date">{from_date}</SVFROMDATE>
      <SVTODATE TYPE="Date">{to_date}</SVTODATE>
      <EXPLODEFLAG>Yes</EXPLODEFLAG>
    </STATICVARIABLES>
  </REQUESTDESC></EXPORTDATA></BODY></ENVELOPE>"""
    r = requests.post(host, data=body.encode("utf-8"), timeout=240)
    if r.status_code != 200:
        raise SystemExit(f"ERROR fetching trial balance for '{company}': HTTP {r.status_code}")
    try:
        root = ET.fromstring(_sanitize_tally_xml(r.content))
    except ET.ParseError as exc:
        raise SystemExit(f"ERROR parsing trial-balance XML for '{company}': {exc}")
    # Surface a Tally report error rather than silently returning nothing.
    err = root.find(".//LINEERROR")
    if err is not None:
        raise SystemExit(f"ERROR: Tally rejected the Trial Balance request: {_t(err)}")

    out: list[tuple[str, float]] = []
    name: str | None = None
    for el in list(root):
        if el.tag == "DSPACCNAME":
            name = _t(el.find("DSPDISPNAME"))
        elif el.tag == "DSPACCINFO" and name is not None:
            dr = _to_float(_t(el.find("DSPCLDRAMT/DSPCLDRAMTA")))
            cr = _to_float(_t(el.find("DSPCLCRAMT/DSPCLCRAMTA")))
            out.append((name, dr + cr))
            name = None
    return out


def build_parent_map(groups: list[ET.Element]) -> dict[str, str]:
    """name → direct parent name (top-level groups have parent "Primary")."""
    out: dict[str, str] = {}
    for g in groups:
        nm = (g.attrib.get("NAME") or _t(g.find("NAME"))).strip()
        parent = _t(g.find("PARENT"))
        if nm:
            out[nm] = parent
    return out


def build_meta_maps(groups: list[ET.Element]) -> tuple[dict[str, str], dict[str, str]]:
    """Return (guid_by_name, isrevenue_by_name) for groups."""
    guid: dict[str, str] = {}
    isrev: dict[str, str] = {}
    for g in groups:
        nm = (g.attrib.get("NAME") or _t(g.find("NAME"))).strip()
        if nm:
            guid[nm] = _t(g.find("GUID"))
            isrev[nm] = _t(g.find("ISREVENUE"))
    return guid, isrev


def parent_chain(name: str, parent_map: dict[str, str]) -> list[str]:
    """Return the chain from `name` up to its top-level ancestor, [name, ...,
    top]. Stops on cycle or empty parent."""
    chain: list[str] = []
    seen: set[str] = set()
    current = name
    while current and current not in seen:
        chain.append(current)
        seen.add(current)
        current = parent_map.get(current, "")
    return chain


def top_level_of(name: str, parent_map: dict[str, str]) -> str:
    """Topmost *real* ancestor of `name` — the last chain item, skipping
    Tally's "Primary" root pseudo-group. Falls back to `name` itself."""
    chain = parent_chain(name, parent_map)
    for ancestor in reversed(chain):
        if ancestor and ancestor != "Primary":
            return ancestor
    return name


def classify_statement(top_level: str, isrevenue: str, closing_val: float) -> tuple[str, str]:
    """Return (statement, side) for a group given its top-level ancestor.

    `Profit & Loss A/c` is special-cased onto the Balance Sheet (that is how
    Tally presents the accumulated result). First match in STATEMENT_RULES
    then wins. Custom top-level groups fall back to Tally's ISREVENUE flag for
    the statement and the closing-balance sign for the side (Tally exports
    debit as negative: raw < 0 → Dr → Assets/Expense; raw >= 0 → Cr →
    Liabilities/Income)."""
    if top_level == "Profit & Loss A/c":
        return "Balance Sheet", ("Assets" if closing_val < 0 else "Liabilities")
    for names, statement, side in STATEMENT_RULES:
        if top_level in names:
            return statement, side
    is_rev = isrevenue.strip().lower() in ("yes", "1", "true")
    statement = "Profit & Loss" if is_rev else "Balance Sheet"
    is_debit = closing_val < 0
    if statement == "Profit & Loss":
        side = "Expense" if is_debit else "Income"
    else:
        side = "Assets" if is_debit else "Liabilities"
    return statement, side


def _flip_signed(v: float) -> str:
    """Tally raw → display: negate. Positive = Dr, negative = Cr."""
    flipped = -v
    if flipped == 0:
        flipped = 0.0
    return f"{flipped:.2f}"


def project_columns(rows: list[dict[str, Any]], columns: list[Column]) -> list[dict[str, Any]]:
    keys = [c.key for c in columns]
    return [{k: r.get(k, "") for k in keys} for r in rows]


def main() -> int:
    parser = argparse.ArgumentParser(description=f"Fetch Tally {STATEMENT} (group level) as JSON.")
    parser.add_argument("--company", required=True, help="Exact name of the single loaded company to fetch.")
    parser.add_argument("--as-of", dest="as_of", default="", help="Statement date, DD-MM-YYYY. Default: today.")
    parser.add_argument("--output", required=True, help="Output JSON path (e.g. .tmp/balance_sheet_<ts>.json)")
    args = parser.parse_args()

    as_of_dmy = args.as_of.strip() or _dt.date.today().strftime("%d-%m-%Y")
    try:
        to_date = _dmy_to_yyyymmdd(as_of_dmy)
    except ValueError:
        raise SystemExit(f"ERROR: --as-of must be DD-MM-YYYY, got '{as_of_dmy}'.")
    from_date = fy_start_yyyymmdd(as_of_dmy)

    load_dotenv()
    host = os.environ.get("TALLY_HOST", "http://localhost:9000").rstrip("/")

    columns = load_columns()
    company_map = load_companies()

    loaded = list_companies(host)
    if not loaded:
        raise SystemExit("ERROR: no companies are loaded in Tally. Load the company and retry.")

    raw_company = args.company.strip()
    if raw_company not in loaded:
        raise SystemExit(
            f"ERROR: company '{raw_company}' is not loaded in Tally.\n"
            f"Loaded companies: {loaded}"
        )

    if raw_company in company_map:
        display_company, location = company_map[raw_company]
    else:
        print(
            f"WARNING: '{raw_company}' is not in reference/companies.md — "
            "using raw name and blank location. Add a row to the mapping to fix this.",
            file=sys.stderr,
        )
        display_company, location = raw_company, ""

    groups = fetch_groups_meta(host, raw_company)
    parent_map = build_parent_map(groups)
    guid_by_name, isrev_by_name = build_meta_maps(groups)
    group_names = set(parent_map)

    tb = fetch_trial_balance(host, raw_company, from_date, to_date)

    all_rows: list[dict[str, Any]] = []
    other_statement = 0
    seen: set[str] = set()
    for name, net_raw in tb:
        if name not in group_names or name in seen:
            continue  # keep groups only, dedupe by name
        seen.add(name)
        top_level = top_level_of(name, parent_map)
        statement, side = classify_statement(top_level, isrev_by_name.get(name, ""), net_raw)
        if statement != STATEMENT:
            other_statement += 1
            continue
        all_rows.append({
            "company": display_company,
            "location": location,
            "as_of_date": as_of_dmy,
            "group_id": guid_by_name.get(name) or name,
            "group_name": name,
            "parent_group": parent_map.get(name, ""),
            "primary_group": top_level,
            "statement": statement,
            "side": side,
            "closing_balance": _flip_signed(net_raw),
            "created_at": "",
            "updated_at": "",
        })

    # Tally presents the running result as a "Profit & Loss A/c" line on the
    # Balance Sheet = its accumulated opening + the current-year P&L (the open
    # Income/Expense groups). It is a special account (not a Group), so it was
    # filtered out above. Re-add it as the balancing line so the statement
    # ties out (Assets total = Liabilities total).
    if STATEMENT == "Balance Sheet":
        pl_ac_raw = 0.0
        current_year_raw = 0.0
        seen_top: set[str] = set()
        for nm, net in tb:
            if nm in seen_top:
                continue
            seen_top.add(nm)
            if nm == "Profit & Loss A/c":
                pl_ac_raw += net
                continue
            if parent_map.get(nm) == "Primary":
                st, _ = classify_statement(nm, isrev_by_name.get(nm, ""), net)
                if st == "Profit & Loss":
                    current_year_raw += net
        combined = pl_ac_raw + current_year_raw
        all_rows = [r for r in all_rows if r["group_name"] != "Profit & Loss A/c"]
        all_rows.append({
            "company": display_company,
            "location": location,
            "as_of_date": as_of_dmy,
            "group_id": guid_by_name.get("Profit & Loss A/c") or "Profit & Loss A/c",
            "group_name": "Profit & Loss A/c",
            "parent_group": "Primary",
            "primary_group": "Profit & Loss A/c",
            "statement": "Balance Sheet",
            "side": "Assets" if combined < 0 else "Liabilities",
            "closing_balance": _flip_signed(combined),
            "created_at": "",
            "updated_at": "",
        })

    rows = project_columns(all_rows, columns)

    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(rows, indent=2, ensure_ascii=False), encoding="utf-8")

    summary = {
        "rows": len(rows),
        "statement": STATEMENT,
        "company": f"{display_company} / {location}",
        "as_of": as_of_dmy,
        "excluded_other_statement": other_statement,
        "source": "Trial Balance report (EXPLODEFLAG=Yes)",
        "output": str(out_path),
    }
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
