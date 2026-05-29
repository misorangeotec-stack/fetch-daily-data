"""Shared schema/config loaders for the tally-salescreditnote-sync-outstanding skill.

Parses the human-editable markdown reference files inside ``reference/``:

* ``columns.md``   → list of ``Column`` tuples (sheet column layout)
* ``companies.md`` → dict of ``raw Tally name → (display company, location)``

Both files use the same simple format: a markdown table under a
``## <SectionName>`` heading. To add/remove/reorder, edit the markdown — no
Python changes needed.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Tuple


@dataclass(frozen=True)
class Column:
    key: str          # JSON key used in .tmp/credit_notes_*.json
    header: str       # Exact text written to row 1 of the Google Sheet
    source: str       # Free-text Tally source hint (for documentation)


REFERENCE_DIR = Path(__file__).resolve().parent.parent / "reference"
SCHEMA_FILE = REFERENCE_DIR / "columns.md"
COMPANIES_FILE = REFERENCE_DIR / "companies.md"


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


def load_companies(path: Path = COMPANIES_FILE) -> Dict[str, Tuple[str, str]]:
    rows = _parse_md_table(path, "Companies", ("tally_name", "company", "location"))
    out: dict[str, tuple[str, str]] = {}
    for r in rows:
        tally_name = r["tally_name"]
        if tally_name in out:
            raise ValueError(f"Duplicate tally_name '{tally_name}' in {path}.")
        out[tally_name] = (r["company"], r["location"])
    return out


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

    rows: list[dict[str, str]] = []
    for row in table_rows[2:]:
        cells = _split_row(row)
        if len(cells) < len(expected_header) or not cells[0]:
            continue
        rows.append({key: cells[i] for i, key in enumerate(expected_lower)})

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
