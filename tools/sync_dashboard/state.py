"""Persistent sync state: last-sync watermarks + step duration history for ETA.

State file lives next to this module: tools/sync_dashboard/sync_state.json
"""

from __future__ import annotations

import json
import statistics
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

STATE_PATH = Path(__file__).resolve().parent / "sync_state.json"

MASTERS: list[str] = ["sales", "salescreditnote", "salesdebitnote", "salesjournal", "bankreceipt", "chequereturn", "credit_limit", "ledger_master", "stock_item_master"]

DEFAULT_DURATIONS_SECONDS: dict[str, dict[str, float]] = {
    # bootstrap ETA when no history exists. Tuned to roughly real durations.
    "sales":             {"fetch": 45, "push": 10},
    "salescreditnote":   {"fetch": 30, "push": 8},
    "salesdebitnote":    {"fetch": 30, "push": 8},
    "salesjournal":      {"fetch": 30, "push": 8},
    "bankreceipt":       {"fetch": 35, "push": 10},
    "chequereturn":      {"fetch": 30, "push": 8},
    "credit_limit":      {"fetch": 60, "push": 12},
    "ledger_master":     {"fetch": 60, "push": 15},
    "stock_item_master": {"fetch": 60, "push": 15},
}

IST = timezone(timedelta(hours=5, minutes=30))
HISTORY_WINDOW = 10


def _empty_state() -> dict[str, Any]:
    return {
        "last_sync": {m: {} for m in MASTERS},
        "step_durations": {},
        "defaults_seconds": DEFAULT_DURATIONS_SECONDS,
    }


def load_state() -> dict[str, Any]:
    if not STATE_PATH.exists():
        return _empty_state()
    try:
        data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return _empty_state()
    # Backfill any missing top-level keys so downstream code can rely on them.
    base = _empty_state()
    for k, v in base.items():
        data.setdefault(k, v)
    for m in MASTERS:
        data["last_sync"].setdefault(m, {})
    return data


def save_state(state: dict[str, Any]) -> None:
    STATE_PATH.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")


def now_ist() -> datetime:
    return datetime.now(IST)


def record_step_duration(state: dict[str, Any], master: str, phase: str, company: str, seconds: float) -> None:
    """Append a duration to both the company-specific and cross-company keys, capped at HISTORY_WINDOW."""
    for key in (f"{master}:{phase}:{company}", f"{master}:{phase}:*"):
        bucket = state["step_durations"].setdefault(key, [])
        bucket.append(round(float(seconds), 2))
        if len(bucket) > HISTORY_WINDOW:
            del bucket[: len(bucket) - HISTORY_WINDOW]


def record_completion(state: dict[str, Any], master: str, company: str, to_date: str | None) -> None:
    state["last_sync"].setdefault(master, {})[company] = {
        "to_date": to_date,
        "completed_at": now_ist().isoformat(timespec="seconds"),
    }


def get_last_sync(state: dict[str, Any], master: str, company: str) -> dict[str, Any] | None:
    return state["last_sync"].get(master, {}).get(company)


def estimate_step_seconds(state: dict[str, Any], master: str, phase: str, company: str) -> float:
    """Best-effort estimate. Order: company-specific median → cross-company median → hardcoded default."""
    sd = state.get("step_durations", {})
    for key in (f"{master}:{phase}:{company}", f"{master}:{phase}:*"):
        history = sd.get(key)
        if history:
            return float(statistics.median(history))
    return float(state.get("defaults_seconds", DEFAULT_DURATIONS_SECONDS).get(master, {}).get(phase, 30))
