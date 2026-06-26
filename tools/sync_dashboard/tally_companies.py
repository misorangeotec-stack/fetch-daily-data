"""Lightweight wrapper around list_companies() shipped by every existing skill.

We import the function from one of the existing skill folders rather than
duplicating the XML query, so the dashboard stays in sync with the skills.
"""

from __future__ import annotations

import importlib.util
import os
import re
import sys
from pathlib import Path

from dotenv import dotenv_values, load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SKILL_LIST_COMPANIES = (
    PROJECT_ROOT
    / ".claude"
    / "skills"
    / "tally-sales-sync-outstanding"
    / "tools"
    / "list_tally_companies.py"
)


def _load_helper():
    spec = importlib.util.spec_from_file_location(
        "_dashboard_list_tally_companies", SKILL_LIST_COMPANIES
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load helper at {SKILL_LIST_COMPANIES}")
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault(spec.name, module)
    spec.loader.exec_module(module)
    return module


def get_tally_host() -> str:
    load_dotenv(PROJECT_ROOT / ".env")
    return os.environ.get("TALLY_HOST", "http://localhost:9000").rstrip("/")


def list_loaded_companies() -> list[str]:
    """Return raw Tally company names. Raises on connection / parse failure."""
    helper = _load_helper()
    return helper.list_companies(get_tally_host())


_CO_KEY_RE = re.compile(r"^TALLY_CO_(.+)_(NAME|NUMBER)$")


def company_number_map() -> dict[str, str]:
    """Map each company's exact Tally name -> its number, parsed from the project
    .env (the TALLY_CO_<key>_NAME / _NUMBER pairs). Used only to annotate the
    sidebar; returns {} on any failure so the UI degrades to names-only.

    The .env is the source of truth here, not Tally — the number is the
    operator-assigned company number, which Tally's company collection does not
    expose over the HTTP/XML gateway.
    """
    try:
        vals = dotenv_values(PROJECT_ROOT / ".env")
    except Exception:
        return {}

    names: dict[str, str] = {}
    numbers: dict[str, str] = {}
    for key, value in vals.items():
        if not value:
            continue
        m = _CO_KEY_RE.match(key)
        if not m:
            continue
        co_key, field = m.group(1), m.group(2)
        (names if field == "NAME" else numbers)[co_key] = value.strip()

    return {name: numbers[k] for k, name in names.items() if k in numbers}


def inactive_company_numbers() -> set[str]:
    """Company numbers flagged as old / closed in the project .env
    (TALLY_INACTIVE_COMPANIES — comma-separated numbers). These are listed in
    the dashboard but left unchecked by default. Returns an empty set on any
    failure or when the var is missing, so every company defaults to active.
    """
    try:
        vals = dotenv_values(PROJECT_ROOT / ".env")
    except Exception:
        return set()
    raw = (vals.get("TALLY_INACTIVE_COMPANIES") or "").strip()
    return {n.strip() for n in raw.split(",") if n.strip()}
