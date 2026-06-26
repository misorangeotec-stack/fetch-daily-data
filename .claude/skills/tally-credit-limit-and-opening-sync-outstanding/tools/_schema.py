"""Shared schema/config loaders for the tally-credit-limit-and-opening-sync-outstanding skill.

Parses the human-editable markdown reference files inside ``reference/``:

* ``columns.md``        → list of ``Column`` tuples (sheet column layout)
* ``companies.md``      → dict of ``raw Tally name → (display company, location)``
* ``sales_persons.md``  → dict of ``ledger name → sales person``

All three files use the same simple format: a markdown table under a
``## <SectionName>`` heading. To add/remove/reorder, edit the markdown — no
Python changes needed.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Tuple


@dataclass(frozen=True)
class Column:
    key: str          # JSON key used in .tmp/credit_limits_*.json
    header: str       # Exact text written to row 1 of the Google Sheet
    source: str       # Free-text Tally source hint (for documentation)


REFERENCE_DIR = Path(__file__).resolve().parent.parent / "reference"
SCHEMA_FILE = REFERENCE_DIR / "columns.md"
COMPANIES_FILE = REFERENCE_DIR / "companies.md"
SALES_PERSONS_FILE = REFERENCE_DIR / "sales_persons.md"
OPENING_OVERRIDES_FILE = REFERENCE_DIR / "opening_overrides.md"
EXCLUDED_LEDGERS_FILE = REFERENCE_DIR / "excluded_ledgers.md"


def load_columns(path: Path = SCHEMA_FILE) -> List[Column]:
    rows = _parse_md_table(path, "Columns", ("key", "column", "tally_source"))
    columns: list[Column] = []
    seen_keys: set[str] = set()
    for r in rows:
        key = r["key"]
        if key in seen_keys:
            raise ValueError(f"Duplicate key '{key}' in {path}.")
        seen_keys.add(key)
        columns.append(Column(key=key, header=r["column"], source=r["tally_source"]))
    return columns


def load_companies(path: Path = COMPANIES_FILE) -> Dict[str, Tuple[str, str, str]]:
    """Map raw Tally company name → (display company, location, apr25_opening mode).

    ``apr25_opening`` is an OPTIONAL 4th column. Value ``zero`` forces every
    ledger's "Opening Apr-25" to 0 for that company — used for FY-rollover
    *continuation* books (e.g. the Noida Enterprises FY26-27 book opened
    1-Apr-2026) whose Tally OPENINGBALANCE is really the carried-forward FY26
    opening, NOT a true 1-Apr-2025 opening. Without this, the fetcher would
    mislabel that carry-forward into the Apr-25 column (== Apr-26), phantom-
    inflating opening balance. Any other/blank value keeps Tally's OPENINGBALANCE.
    """
    rows = _parse_md_table(path, "Companies", ("tally_name", "company", "location"))
    out: dict[str, tuple[str, str, str]] = {}
    for r in rows:
        tally_name = r["tally_name"]
        if tally_name in out:
            raise ValueError(f"Duplicate tally_name '{tally_name}' in {path}.")
        out[tally_name] = (r["company"], r["location"], r.get("apr25_opening", "").strip().lower())
    return out


def load_sales_persons(path: Path = SALES_PERSONS_FILE) -> Dict[str, str]:
    rows = _parse_md_table(path, "Sales Persons", ("name", "sales_person"))
    out: dict[str, str] = {}
    for r in rows:
        name = r["name"]
        if name in out:
            # Last-write-wins on dupes; keeps the file from crashing the run
            # if the user hand-edits and accidentally duplicates a name.
            pass
        out[name] = r["sales_person"]
    return out


def load_opening_overrides(path: Path = OPENING_OVERRIDES_FILE) -> Dict[Tuple[str, str, str], Tuple[str, str]]:
    """Map (company, location, ledger UPPER) → (apr25_amount, apr25_drcr).

    Per-ledger overrides applied AFTER the company-level ``apr25_opening`` flag
    in the fetcher, so a specific ledger's real opening wins over a company's
    blanket ``zero`` (see reference/opening_overrides.md). Returns ``{}`` if the
    file is absent (the override is optional).
    """
    if not path.exists():
        return {}
    rows = _parse_md_table(path, "Overrides", ("company", "location", "ledger", "apr25_amount", "apr25_drcr"))
    out: dict[tuple[str, str, str], tuple[str, str]] = {}
    for r in rows:
        key = (r["company"].strip(), r["location"].strip(), r["ledger"].strip().upper())
        out[key] = (r["apr25_amount"].strip(), r["apr25_drcr"].strip())
    return out


def load_excluded_ledgers(path: Path = EXCLUDED_LEDGERS_FILE) -> set[Tuple[str, str, str]]:
    """Return the set of (company, location, ledger UPPER) ledgers to SKIP.

    The fetcher drops any ledger matching this set even if Tally files it under
    Sundry Debtors — for GL accruals / control accounts that aren't real debtors
    (see reference/excluded_ledgers.md). Returns an empty set if the file is absent.
    """
    if not path.exists():
        return set()
    rows = _parse_md_table(path, "Excluded", ("company", "location", "ledger"))
    return {(r["company"].strip(), r["location"].strip(), r["ledger"].strip().upper()) for r in rows}


def _parse_md_table(path: Path, section: str, expected_header: tuple[str, ...]) -> list[dict[str, str]]:
    text = path.read_text(encoding="utf-8")
    section_marker = f"## {section.lower()}"

    in_section = False
    table_rows: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("## "):
            in_section = stripped.lower() == section_marker
            continue
        if in_section and stripped.startswith("|"):
            table_rows.append(stripped)

    if len(table_rows) < 3:
        raise ValueError(
            f"Could not find a '## {section}' table in {path}. "
            f"Expected header cells: {list(expected_header)}."
        )

    header_cells = [c.lower() for c in _split_row(table_rows[0])]
    expected_lower = [h.lower() for h in expected_header]
    if header_cells[: len(expected_lower)] != expected_lower:
        raise ValueError(
            f"Unexpected header row in {path}: {header_cells}. "
            f"First {len(expected_lower)} cells must be: {list(expected_header)}."
        )

    # Key each row by the table's ACTUAL header cells (not just the expected
    # ones), so optional trailing columns — e.g. companies.md's apr25_opening —
    # are captured. Loaders that don't care about extras simply ignore them.
    rows: list[dict[str, str]] = []
    for row in table_rows[2:]:
        cells = _split_row(row)
        if len(cells) < len(expected_header) or not cells[0]:
            continue
        rows.append({header_cells[i]: cells[i] for i in range(len(header_cells)) if i < len(cells)})

    if not rows:
        raise ValueError(f"No data rows found in '## {section}' table in {path}.")
    return rows


def _split_row(row: str) -> list[str]:
    return [p.strip() for p in row.strip().strip("|").split("|")]


if __name__ == "__main__":
    import json
    print("--- columns ---")
    print(json.dumps([c.__dict__ for c in load_columns()], indent=2))
    print("\n--- companies ---")
    print(json.dumps({k: list(v) for k, v in load_companies().items()}, indent=2))
    sp = load_sales_persons()
    print(f"\n--- sales_persons --- ({len(sp)} entries)")
    print(json.dumps(dict(list(sp.items())[:5]), indent=2))
