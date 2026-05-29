"""Sequential subprocess driver for the Tally sync dashboard.

Yields progress events that the Streamlit app consumes to update its UI.
Date-range syncs are chunked per calendar month to avoid Tally's 180s read timeout.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterator, Literal

import state as state_mod  # streamlit runs app.py with this dir on sys.path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SKILLS_ROOT = PROJECT_ROOT / ".claude" / "skills"
TMP_ROOT = PROJECT_ROOT / ".tmp"

# The Orange Receivables Hub (sibling project) holds reconcile.py / process_data.py / self_heal.py.
# Hub steps (P5) run there with cwd=HUB so the Hub's .env (Supabase, sheet URLs, Tally) loads.
HUB_ROOT = Path(os.environ.get("RECEIVABLES_HUB_PATH", r"d:/Agentic AI Tools/Orange Receivables Hub"))

Phase = Literal["fetch", "push"]

# ---- per-master script registry ----------------------------------------------------------------

@dataclass(frozen=True)
class MasterDef:
    key: str                  # state-file key
    label: str                # human-readable, used in UI
    skill_dir: str            # folder under .claude/skills/
    fetch_script: str         # filename inside <skill>/tools/
    push_script: str
    date_range: bool          # False => master snapshot, ignore date controls
    tmp_prefix: str           # prefix for .tmp output files

MASTERS: dict[str, MasterDef] = {
    "sales": MasterDef(
        "sales", "Sales", "tally-sales-sync-outstanding",
        "fetch_tally_sales.py", "push_sales_to_sheet.py",
        date_range=True, tmp_prefix="sales",
    ),
    "salescreditnote": MasterDef(
        "salescreditnote", "Sales Credit Note", "tally-salescreditnote-sync-outstanding",
        "fetch_tally_credit_notes.py", "push_credit_notes_to_sheet.py",
        date_range=True, tmp_prefix="cn",
    ),
    "salesdebitnote": MasterDef(
        "salesdebitnote", "Sales Debit Note", "tally-salesdebitnote-sync-outstanding",
        "fetch_tally_debit_notes.py", "push_debit_notes_to_sheet.py",
        date_range=True, tmp_prefix="dn",
    ),
    "salesjournal": MasterDef(
        "salesjournal", "Sales Journal", "tally-salesjournal-sync-outstanding",
        "fetch_tally_journals.py", "push_journals_to_sheet.py",
        date_range=True, tmp_prefix="journal",
    ),
    "bankreceipt": MasterDef(
        "bankreceipt", "Bank Receipt", "tally-bankreceipt-sync-outstanding",
        "fetch_tally_bankreceipt.py", "push_bankreceipt_to_sheet.py",
        date_range=True, tmp_prefix="bankreceipt",
    ),
    "chequereturn": MasterDef(
        "chequereturn", "Cheque Return", "tally-chequereturn-sync-outstanding",
        "fetch_tally_chqreturn.py", "push_chqreturn_to_sheet.py",
        date_range=True, tmp_prefix="chq",
    ),
    "credit_limit": MasterDef(
        "credit_limit", "Credit Limit & Opening", "tally-credit-limit-and-opening-sync-outstanding",
        "fetch_tally_credit_limits.py", "push_credit_limits_to_sheet.py",
        date_range=False, tmp_prefix="credit_limits",
    ),
    "ledger_master": MasterDef(
        "ledger_master", "Ledger Master", "tally-ledger-master-sync",
        "fetch_tally_ledger_master.py", "push_ledger_master_to_sheet.py",
        date_range=False, tmp_prefix="ledger_master",
    ),
    "stock_item_master": MasterDef(
        "stock_item_master", "Stock Item Master", "tally-stock-item-master-sync",
        "fetch_tally_stock_item_master.py", "push_stock_item_master_to_sheet.py",
        date_range=False, tmp_prefix="stock_item_master",
    ),
    "billwise": MasterDef(
        "billwise", "Bill-wise Outstanding", "tally-billwise-outstanding-sync",
        "fetch_tally_billwise.py", "push_billwise_to_sheet.py",
        date_range=False, tmp_prefix="billwise",
    ),
}

UI_ORDER = ["sales", "salescreditnote", "salesdebitnote", "salesjournal", "bankreceipt", "chequereturn", "credit_limit", "ledger_master", "stock_item_master", "billwise"]


# ---- date / chunk helpers ----------------------------------------------------------------------

def fy_start_for(d: date) -> date:
    """Indian FY starts 1 April."""
    return date(d.year, 4, 1) if d.month >= 4 else date(d.year - 1, 4, 1)


def parse_dmy(s: str) -> date:
    return datetime.strptime(s, "%d-%m-%Y").date()


def fmt_dmy(d: date) -> str:
    return d.strftime("%d-%m-%Y")


def month_chunks(start: date, end: date) -> list[tuple[date, date]]:
    """Split [start, end] into per-calendar-month [from, to] inclusive sub-ranges."""
    if start > end:
        return []
    chunks: list[tuple[date, date]] = []
    cur = start
    while cur <= end:
        if cur.month == 12:
            month_end = date(cur.year, 12, 31)
        else:
            month_end = date(cur.year, cur.month + 1, 1) - timedelta(days=1)
        chunks.append((cur, min(month_end, end)))
        cur = month_end + timedelta(days=1)
    return chunks


# ---- step model --------------------------------------------------------------------------------

@dataclass
class Step:
    master: str
    company: str
    phase: Phase
    chunk_idx: int = 0          # 0-based index within (master, company)
    chunk_total: int = 1
    chunk_label: str = ""       # e.g. "Apr 2026" or "" for non-chunked
    from_date: str | None = None  # DD-MM-YYYY
    to_date: str | None = None
    input_file: str | None = None  # for push only
    reconcile: bool = False
    status: Literal["pending", "running", "done", "error", "skipped"] = "pending"
    elapsed: float = 0.0
    started_at: float | None = None
    error: str | None = None
    summary: dict[str, Any] | None = None  # parsed JSON from script's final stdout line
    # ── Hub steps (P5): reconcile / process_data / self_heal run in the Receivables Hub. ──
    # step_type "tally" = the normal per-master fetch/push (uses master/company/phase above).
    # Any other value = a Hub step described by `argv` (a full command), run with cwd=HUB and
    # the parent env (the Hub script loads its own .env). Hub steps NEVER advance the daily
    # sync watermark (record_completion is gated on step_type=="tally").
    step_type: str = "tally"
    label: str = ""             # human-readable label for hub steps (master is "" for them)
    argv: list[str] | None = None  # explicit command for a hub step (overrides _build_cmd)
    cwd: str | None = None      # working dir override (defaults to PROJECT_ROOT)
    env_extra: dict[str, str] | None = None  # extra env vars merged over os.environ for this step


@dataclass
class MasterPlan:
    master: str
    mode: Literal["last_pending", "specific", "snapshot"]
    from_date: str | None = None  # for "specific" only; DD-MM-YYYY
    to_date: str | None = None
    reconcile: bool = False


# ---- step builder ------------------------------------------------------------------------------

def build_steps(state: dict[str, Any], plans: list[MasterPlan], companies: list[str], today: date | None = None) -> list[Step]:
    if today is None:
        today = state_mod.now_ist().date()
    steps: list[Step] = []
    fy_start = fy_start_for(today)

    for plan in plans:
        m = MASTERS[plan.master]
        for company in companies:
            if not m.date_range:
                # Master snapshot — single fetch + single push, no date args.
                tmp_in = _tmp_path(m, company, "snap")
                steps.append(Step(plan.master, company, "fetch", 0, 1, "snapshot", input_file=str(tmp_in)))
                steps.append(Step(plan.master, company, "push", 0, 1, "snapshot", input_file=str(tmp_in)))
                continue

            # Determine effective date range for this (master, company).
            if plan.mode == "specific":
                start = parse_dmy(plan.from_date)  # type: ignore[arg-type]
                end = parse_dmy(plan.to_date)      # type: ignore[arg-type]
            else:  # "last_pending"
                last = state_mod.get_last_sync(state, plan.master, company)
                if last and last.get("to_date"):
                    start = parse_dmy(last["to_date"]) + timedelta(days=1)
                else:
                    start = fy_start
                end = today
            if start > end:
                # Nothing to sync — already up to date.
                continue

            chunks = month_chunks(start, end)
            for idx, (cs, ce) in enumerate(chunks):
                chunk_label = cs.strftime("%b %Y")
                tmp_in = _tmp_path(m, company, f"{cs:%Y%m}")
                steps.append(Step(
                    plan.master, company, "fetch", idx, len(chunks), chunk_label,
                    from_date=fmt_dmy(cs), to_date=fmt_dmy(ce), input_file=str(tmp_in),
                ))
                steps.append(Step(
                    plan.master, company, "push", idx, len(chunks), chunk_label,
                    from_date=fmt_dmy(cs), to_date=fmt_dmy(ce), input_file=str(tmp_in),
                    reconcile=plan.reconcile,
                ))
    return steps


def _tmp_path(m: MasterDef, company: str, tag: str) -> Path:
    safe_company = re.sub(r"[^A-Za-z0-9_-]+", "_", company)[:32]
    ts = int(time.time())
    return TMP_ROOT / f"dashboard_{m.tmp_prefix}_{safe_company}_{tag}_{ts}.json"


# ---- Hub steps (P5): reconcile / process_data / self_heal in the Receivables Hub ----------------

def step_label(step: Step) -> str:
    """Human-readable label for a step — hub-safe (hub steps have master='' which isn't in MASTERS)."""
    if step.step_type != "tally":
        return step.label or step.step_type
    return MASTERS[step.master].label


def hub_step(step_type: str, label: str, argv: list[str], env_extra: dict[str, str] | None = None) -> Step:
    """A Step that runs a Hub script (cwd=HUB). Carries an explicit argv; not tied to a master."""
    return Step(master="", company="", phase="push", step_type=step_type, label=label,
                argv=argv, cwd=str(HUB_ROOT), env_extra={"PYTHONUTF8": "1", **(env_extra or {})})


def process_data_step() -> Step:
    """Hub step: re-run process_data.py (OUTPUT_MODE=supabase) to refresh the live dashboard."""
    py = sys.executable or "python"
    return hub_step("hub_process_data", "Push to dashboard (process_data → Supabase)",
                    [py, str(HUB_ROOT / "scripts" / "process_data.py")],
                    env_extra={"OUTPUT_MODE": "supabase"})


def reconcile_step(as_of: str, tally_cache: str, ledger_map: str,
                   extra: list[str] | None = None) -> Step:
    """Hub step: run reconcile.py read-only for the given as-of (cache + ledger-map required)."""
    py = sys.executable or "python"
    argv = [py, str(HUB_ROOT / "scripts" / "reconcile.py"), "--as-of", as_of,
            "--tally-cache", tally_cache, "--ledger-map", ledger_map] + (extra or [])
    return hub_step("hub_reconcile", f"Reconcile (as-of {as_of})", argv)


def heal_step(report: str, as_of: str, tally_cache: str, prior_cache: str,
              heal_ids: str, execute: bool, extra: list[str] | None = None) -> Step:
    """Hub step: run self_heal.py. execute=False → dry-run plan; True → fetch + GUID-heal."""
    py = sys.executable or "python"
    argv = [py, str(HUB_ROOT / "scripts" / "self_heal.py"), "--report", report,
            "--as-of", as_of, "--tally-cache", tally_cache, "--prior-cache", prior_cache,
            "--heal-ids", heal_ids, "--json"]
    if execute:
        argv.append("--execute")
    argv += (extra or [])
    return hub_step("hub_heal", f"Heal ({'execute' if execute else 'plan'})", argv)


def cache_build_step(as_of: str, master_dump: str, out_cache: str,
                     prior_cache: str | None = None) -> Step:
    """Hub step: build the Tally ledger cache for an as-of via the P0b generic builder."""
    py = sys.executable or "python"
    argv = [py, str(HUB_ROOT / "scripts" / "stage0_build_ledger_cache_generic.py"),
            "--as-of", as_of, "--master", master_dump, "--out", out_cache]
    if prior_cache:
        argv += ["--prior", prior_cache]
    return hub_step("hub_cache_build", f"Build Tally cache (as-of {as_of})", argv)


# ---- ETA -----------------------------------------------------------------------------------

def estimate_total_seconds(state: dict[str, Any], steps: list[Step]) -> float:
    return sum(state_mod.estimate_step_seconds(state, s.master, s.phase, s.company) for s in steps if s.status in ("pending", "running"))


# ---- runner ---------------------------------------------------------------------------------

@dataclass
class ProgressEvent:
    kind: Literal["start", "log", "step_done", "all_done", "cancelled"]
    step_index: int | None = None
    line: str | None = None
    extras: dict[str, Any] = field(default_factory=dict)


class Runner:
    """Runs the step list sequentially. Use `events()` to iterate progress events."""

    def __init__(self, state: dict[str, Any], steps: list[Step], auto_process_data: bool = False):
        self.state = state
        self.steps = steps
        # P4: when True, after a clean sync that changed rows, auto-append + run a process_data
        # hub step so the live dashboard reflects the new sheet data in one click.
        self.auto_process_data = auto_process_data
        self._events: deque[ProgressEvent] = deque()
        self._lock = threading.Lock()
        self._current_proc: subprocess.Popen | None = None
        self._cancel = threading.Event()
        self._done = threading.Event()
        self._thread: threading.Thread | None = None
        self._log_tail: deque[str] = deque(maxlen=200)

    # ---- public API ----------------------------------------------------------------------

    def start(self) -> None:
        TMP_ROOT.mkdir(parents=True, exist_ok=True)
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def cancel(self) -> None:
        self._cancel.set()
        with self._lock:
            proc = self._current_proc
        if proc and proc.poll() is None:
            try:
                proc.terminate()
            except Exception:
                pass

    def is_running(self) -> bool:
        return not self._done.is_set()

    def drain_events(self) -> list[ProgressEvent]:
        with self._lock:
            out = list(self._events)
            self._events.clear()
        return out

    def log_tail(self, n: int = 30) -> list[str]:
        with self._lock:
            return list(self._log_tail)[-n:]

    # ---- runner internals ----------------------------------------------------------------

    def _emit(self, ev: ProgressEvent) -> None:
        with self._lock:
            self._events.append(ev)

    def _log(self, line: str) -> None:
        with self._lock:
            self._log_tail.append(line)

    def _run(self) -> None:
        try:
            for idx, step in enumerate(self.steps):
                if self._cancel.is_set():
                    for s in self.steps[idx:]:
                        if s.status == "pending":
                            s.status = "skipped"
                    self._emit(ProgressEvent(kind="cancelled"))
                    return
                self._run_step(idx, step)
            # P4: auto-push to the dashboard after a clean sync that changed rows.
            self._maybe_auto_process_data()
        finally:
            self._done.set()
            self._emit(ProgressEvent(kind="all_done"))

    def _sync_changed_rows(self) -> bool:
        """True if any tally PUSH step actually changed the sheet (appended>0 OR deleted>0).

        There is no "updated" counter — an edit shows as delete+insert (P4 / audit #9). Checks
        both the top-level counts and the sales `details` (Register) sub-summary.
        """
        for s in self.steps:
            if s.step_type != "tally" or s.phase != "push" or not s.summary:
                continue
            if (s.summary.get("appended") or 0) > 0 or (s.summary.get("deleted") or 0) > 0:
                return True
            d = s.summary.get("details") or {}
            if (d.get("appended") or 0) > 0 or (d.get("deleted") or 0) > 0:
                return True
        return False

    def _maybe_auto_process_data(self) -> None:
        if not self.auto_process_data or self._cancel.is_set():
            return
        if any(s.status == "error" for s in self.steps):
            self._emit(ProgressEvent(kind="log",
                       line="↻ Auto-push skipped: a fetch/push step errored — run process_data manually after review."))
            return
        if not self._sync_changed_rows():
            self._emit(ProgressEvent(kind="log",
                       line="↻ Auto-push skipped: nothing changed (0 appended, 0 deleted)."))
            return
        step = process_data_step()
        self.steps.append(step)
        self._emit(ProgressEvent(kind="log",
                   line="↻ Changes detected — auto-running process_data → Supabase to refresh the dashboard…"))
        self._run_step(len(self.steps) - 1, step)

    def _run_step(self, idx: int, step: Step) -> None:
        step.status = "running"
        step.started_at = time.time()
        self._emit(ProgressEvent(kind="start", step_index=idx))
        is_hub = step.step_type != "tally"
        m = MASTERS[step.master] if not is_hub else None

        cmd = self._build_cmd(m, step)
        self._log(f"$ {' '.join(cmd)}")

        run_cwd = step.cwd or str(PROJECT_ROOT)
        run_env = {**os.environ, **step.env_extra} if step.env_extra else None

        last_json_line: str | None = None
        rc = -1
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                bufsize=1,
                text=True,
                cwd=run_cwd,
                env=run_env,
            )
            with self._lock:
                self._current_proc = proc
            assert proc.stdout is not None
            for raw in proc.stdout:
                line = raw.rstrip("\n")
                self._log(line)
                self._emit(ProgressEvent(kind="log", step_index=idx, line=line))
                if line.startswith("{") and line.rstrip().endswith("}"):
                    last_json_line = line
            rc = proc.wait()
        except Exception as exc:
            step.status = "error"
            step.error = repr(exc)
            self._log(f"!! exception: {exc!r}")
            self._emit(ProgressEvent(kind="step_done", step_index=idx))
            return
        finally:
            with self._lock:
                self._current_proc = None
            if step.started_at is not None:
                step.elapsed = time.time() - step.started_at

        if rc != 0:
            step.status = "error"
            step.error = f"exit code {rc}"
            self._emit(ProgressEvent(kind="step_done", step_index=idx))
            return

        if last_json_line:
            try:
                step.summary = json.loads(last_json_line)
            except json.JSONDecodeError:
                step.summary = None

        # Hub steps (reconcile / process_data / heal) are targeted repairs, NOT the daily sync —
        # they must NOT touch duration history or the completion watermark (P5 / audit #5).
        if step.step_type == "tally":
            state_mod.record_step_duration(self.state, step.master, step.phase, step.company, step.elapsed)
            if step.phase == "push":
                # Watermark advances after a successful per-master push only.
                state_mod.record_completion(self.state, step.master, step.company, step.to_date)
            state_mod.save_state(self.state)

        step.status = "done"
        self._emit(ProgressEvent(kind="step_done", step_index=idx))

    def _build_cmd(self, m: MasterDef | None, step: Step) -> list[str]:
        # Hub steps carry their full command in `argv` (built by the hub_step factories).
        if step.argv is not None:
            return list(step.argv)
        assert m is not None  # tally steps always have a MasterDef
        py = sys.executable or "python"
        tools_dir = SKILLS_ROOT / m.skill_dir / "tools"
        if step.phase == "fetch":
            script = str(tools_dir / m.fetch_script)
            if m.date_range:
                return [
                    py, script,
                    "--from", step.from_date or "",
                    "--to", step.to_date or "",
                    "--companies", step.company,
                    "--output", step.input_file or "",
                ]
            else:
                return [
                    py, script,
                    "--companies", step.company,
                    "--output", step.input_file or "",
                ]
        else:  # push
            cmd = [py, str(tools_dir / m.push_script), "--input", step.input_file or ""]
            if step.reconcile and step.from_date and step.to_date:
                cmd += ["--reconcile", "--from", step.from_date, "--to", step.to_date,
                        "--companies", step.company]
            return cmd
