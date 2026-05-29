"""Lightweight wrapper around list_companies() shipped by every existing skill.

We import the function from one of the existing skill folders rather than
duplicating the XML query, so the dashboard stays in sync with the skills.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

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
