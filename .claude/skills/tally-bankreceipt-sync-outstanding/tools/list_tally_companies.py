"""List companies currently loaded in Tally Prime.

Required env vars (loaded from project .env via python-dotenv):
    TALLY_HOST   e.g. http://localhost:9000

Usage:
    python list_tally_companies.py

Prints a JSON array of company names to stdout. Exits non-zero if Tally is
unreachable or the response can't be parsed.
"""

from __future__ import annotations

import json
import os
import sys
import xml.etree.ElementTree as ET

import requests
from dotenv import load_dotenv


REQUEST_XML = """\
<ENVELOPE>
  <HEADER>
    <VERSION>1</VERSION>
    <TALLYREQUEST>Export</TALLYREQUEST>
    <TYPE>Collection</TYPE>
    <ID>List of Companies</ID>
  </HEADER>
  <BODY>
    <DESC>
      <STATICVARIABLES>
        <SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
      </STATICVARIABLES>
      <TDL>
        <TDLMESSAGE>
          <COLLECTION NAME="List of Companies" ISMODIFY="No">
            <TYPE>Company</TYPE>
            <FETCH>NAME</FETCH>
          </COLLECTION>
        </TDLMESSAGE>
      </TDL>
    </DESC>
  </BODY>
</ENVELOPE>
"""


def list_companies(host: str) -> list[str]:
    try:
        resp = requests.post(host, data=REQUEST_XML.encode("utf-8"), timeout=120)
    except requests.exceptions.ConnectionError as exc:
        raise SystemExit(
            f"ERROR: cannot reach Tally at {host}. "
            "Is Tally Prime running with HTTP/XML enabled? "
            f"({exc})"
        )

    if resp.status_code != 200:
        raise SystemExit(f"ERROR: Tally returned HTTP {resp.status_code}: {resp.text[:300]}")

    try:
        root = ET.fromstring(resp.content)
    except ET.ParseError as exc:
        raise SystemExit(f"ERROR: could not parse Tally XML response: {exc}\n{resp.text[:500]}")

    names: list[str] = []
    for company_el in root.iter("COMPANY"):
        name_attr = company_el.attrib.get("NAME")
        if name_attr:
            names.append(name_attr.strip())
            continue
        name_el = company_el.find("NAME")
        if name_el is not None and name_el.text:
            names.append(name_el.text.strip())

    seen: set[str] = set()
    deduped = [n for n in names if not (n in seen or seen.add(n))]
    return deduped


def main() -> int:
    load_dotenv()
    host = os.environ.get("TALLY_HOST", "http://localhost:9000").rstrip("/")
    companies = list_companies(host)
    json.dump(companies, sys.stdout, indent=2, ensure_ascii=False)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
