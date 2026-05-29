"""Tally Sync Dashboard — Streamlit UI.

Run from project root:
    streamlit run tools/sync_dashboard/app.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import streamlit as st

# Make sibling modules importable when streamlit launches this file as a script.
sys.path.insert(0, str(Path(__file__).resolve().parent))

import state as state_mod  # noqa: E402
from cancellation_audit import MASTER_CONFIG, AuditRunner  # noqa: E402
from orchestrator import (  # noqa: E402
    HUB_ROOT,
    MASTERS,
    PROJECT_ROOT,
    SKILLS_ROOT,
    UI_ORDER,
    MasterPlan,
    Runner,
    build_steps,
    cache_build_step,
    estimate_total_seconds,
    fmt_dmy,
    fy_start_for,
    heal_step,
    reconcile_step,
    step_label,
)
from tally_companies import get_tally_host, list_loaded_companies  # noqa: E402

st.set_page_config(
    page_title="Tally Sync Dashboard",
    page_icon="🟠",
    layout="wide",
    initial_sidebar_state="expanded",
)

ACCENT = "#F97316"          # orange
ACCENT_DARK = "#C2410C"
SUCCESS = "#16A34A"
WARN = "#D97706"
ERR = "#DC2626"
MUTED = "#64748B"


# ------------------------------------------------------------------------------------ styling

def inject_css() -> None:
    st.markdown(
        f"""
        <style>
          /* Tighten default padding so the dashboard feels denser */
          .block-container {{
            padding-top: 1.6rem;
            padding-bottom: 4rem;
            max-width: 1180px;
          }}
          /* Sidebar tweaks */
          section[data-testid="stSidebar"] {{
            background: #FAFAF9;
            border-right: 1px solid #E7E5E4;
          }}
          /* Bordered containers — soften and add hover */
          div[data-testid="stVerticalBlockBorderWrapper"] {{
            border-radius: 14px !important;
            border: 1px solid #E7E5E4 !important;
            box-shadow: 0 1px 0 rgba(0,0,0,0.02);
            transition: border-color .15s ease, box-shadow .15s ease;
          }}
          div[data-testid="stVerticalBlockBorderWrapper"]:hover {{
            border-color: #D6D3D1 !important;
            box-shadow: 0 2px 8px rgba(0,0,0,0.04);
          }}
          /* Primary button — orange accent */
          button[kind="primary"] {{
            background: linear-gradient(180deg, {ACCENT} 0%, {ACCENT_DARK} 100%) !important;
            border: 0 !important;
            font-weight: 600 !important;
            box-shadow: 0 1px 0 rgba(0,0,0,0.06) !important;
          }}
          button[kind="primary"]:hover {{
            filter: brightness(1.05);
          }}
          /* Metric cards */
          div[data-testid="stMetric"] {{
            background: #FFFFFF;
            border: 1px solid #E7E5E4;
            border-radius: 12px;
            padding: 0.85rem 1rem;
          }}
          div[data-testid="stMetricLabel"] p {{
            color: {MUTED};
            font-weight: 600;
            font-size: 0.78rem;
            text-transform: uppercase;
            letter-spacing: 0.04em;
          }}
          /* Progress bar — orange */
          div[data-testid="stProgress"] > div > div > div > div {{
            background: linear-gradient(90deg, {ACCENT} 0%, {ACCENT_DARK} 100%) !important;
          }}
          /* Pills for connection status */
          .pill {{
            display: inline-flex;
            align-items: center;
            gap: 0.4rem;
            padding: 0.2rem 0.65rem;
            border-radius: 999px;
            font-size: 0.82rem;
            font-weight: 600;
            border: 1px solid transparent;
          }}
          .pill.ok    {{ background: #DCFCE7; color: #166534; border-color: #BBF7D0; }}
          .pill.warn  {{ background: #FEF3C7; color: #92400E; border-color: #FDE68A; }}
          .pill.err   {{ background: #FEE2E2; color: #991B1B; border-color: #FECACA; }}
          .pill.muted {{ background: #F5F5F4; color: #57534E; border-color: #E7E5E4; }}
          .pill .dot  {{ width: 8px; height: 8px; border-radius: 50%; background: currentColor; }}
          /* Section labels */
          .section-eyebrow {{
            color: {MUTED};
            font-size: 0.78rem;
            text-transform: uppercase;
            letter-spacing: 0.08em;
            font-weight: 600;
            margin-bottom: 0.25rem;
          }}
          /* Master card heading */
          .master-title {{
            font-size: 1.05rem;
            font-weight: 600;
            color: #1C1917;
          }}
          .master-sub {{
            color: {MUTED};
            font-size: 0.85rem;
          }}
          /* Step list rows */
          .step-row {{
            font-family: ui-monospace, "SF Mono", Menlo, monospace;
            font-size: 0.82rem;
            padding: 0.15rem 0;
          }}
          .step-icon {{
            display: inline-block;
            width: 1.4em;
            text-align: center;
            font-weight: 700;
          }}
          .icon-done {{ color: {SUCCESS}; }}
          .icon-run  {{ color: {ACCENT}; }}
          .icon-err  {{ color: {ERR}; }}
          .icon-skip {{ color: {MUTED}; }}
          .icon-pend {{ color: #D6D3D1; }}
          /* Status header above progress bar */
          .status-header {{
            display: flex;
            align-items: center;
            gap: 0.6rem;
            font-size: 1.5rem;
            font-weight: 700;
            margin-bottom: 0.25rem;
          }}
          .status-header.running  {{ color: {ACCENT_DARK}; }}
          .status-header.complete {{ color: {SUCCESS}; }}
          .status-header.warn     {{ color: {WARN}; }}
          .status-header.err      {{ color: {ERR}; }}
          .status-sub {{ color: {MUTED}; font-size: 0.95rem; margin-bottom: 1rem; }}
          /* Sidebar company checkboxes — let long names wrap, tighter spacing */
          section[data-testid="stSidebar"] div[data-testid="stCheckbox"] label p {{
            white-space: normal !important;
            line-height: 1.25 !important;
            font-size: 0.85rem !important;
          }}
          section[data-testid="stSidebar"] div[data-testid="stCheckbox"] {{
            margin-bottom: 0.15rem;
          }}
        </style>
        """,
        unsafe_allow_html=True,
    )


# ------------------------------------------------------------------------------------ helpers

def fmt_seconds(s: float) -> str:
    s = max(0, int(s))
    if s >= 3600:
        return f"{s // 3600}h {(s % 3600) // 60:02d}m"
    return f"{s // 60:02d}:{s % 60:02d}"


def watermark_html(state: dict, master_key: str, selected_companies: list[str]) -> str:
    """Compact 'last synced' chip — colour reflects health vs. selection."""
    if not selected_companies:
        return f'<span class="pill muted"><span class="dot"></span>No company selected</span>'
    syncs = [state_mod.get_last_sync(state, master_key, c) for c in selected_companies]
    have = [s for s in syncs if s and s.get("completed_at")]
    if not have:
        return f'<span class="pill err"><span class="dot"></span>Never synced</span>'
    oldest = min(s["completed_at"] for s in have)[:16].replace("T", " ")
    if len(have) < len(selected_companies):
        pill = f'<span class="pill warn"><span class="dot"></span>Partial · oldest {oldest}</span>'
    else:
        pill = f'<span class="pill ok"><span class="dot"></span>{oldest}</span>'

    # "Data through" — find oldest to_date across selected companies (most conservative)
    to_date_strs = [s["to_date"] for s in have if s.get("to_date")]
    data_through = ""
    if to_date_strs:
        try:
            oldest_to = min(to_date_strs, key=lambda d: datetime.strptime(d, "%d-%m-%Y"))
            data_through = (
                f'<div style="font-size:0.78rem;color:{MUTED};margin-top:0.3rem;">'
                f'Synced up to <b style="color:#1C1917">{oldest_to}</b>'
                f'</div>'
            )
        except ValueError:
            pass

    return pill + data_through


# ------------------------------------------------------------------------------------ session

def init_session() -> None:
    ss = st.session_state
    ss.setdefault("companies_loaded", None)
    ss.setdefault("companies_error", None)
    ss.setdefault("runner", None)
    ss.setdefault("steps", None)
    ss.setdefault("run_total_estimate", 0.0)
    ss.setdefault("run_started_at", None)
    ss.setdefault("master_selected", {m: False for m in UI_ORDER})
    ss.setdefault("audit_runner", None)
    ss.setdefault("audit_results", None)
    ss.setdefault("audit_log", [])   # list of human-readable progress strings
    # Reconcile & Heal tab (P6)
    ss.setdefault("recon_rows", None)     # routed rows (from self_heal --json plan) of last run
    ss.setdefault("recon_asof", None)     # as-of string of the loaded result
    ss.setdefault("recon_report", None)   # path to the xlsx report
    ss.setdefault("recon_log", [])        # human-readable progress log
    ss.setdefault("recon_heal_sel", {})   # {ledger_id: bool} per-row heal selection


def load_companies_into_session() -> None:
    try:
        st.session_state.companies_loaded = list_loaded_companies()
        st.session_state.companies_error = None
    except SystemExit as e:
        st.session_state.companies_loaded = []
        st.session_state.companies_error = str(e)
    except Exception as e:
        st.session_state.companies_loaded = []
        st.session_state.companies_error = repr(e)


# ------------------------------------------------------------------------------------ sidebar

def render_sidebar() -> list[str]:
    if st.session_state.companies_loaded is None:
        load_companies_into_session()

    with st.sidebar:
        st.markdown("### 🟠 Tally Sync")
        st.caption("Outstanding-report orchestrator")

        st.markdown("&nbsp;", unsafe_allow_html=True)

        st.markdown('<div class="section-eyebrow">Tally connection</div>', unsafe_allow_html=True)
        host = get_tally_host()
        if st.session_state.companies_error:
            st.markdown(f'<span class="pill err"><span class="dot"></span>Not reachable</span>', unsafe_allow_html=True)
            with st.expander("Error details"):
                st.code(st.session_state.companies_error, language="text")
        else:
            n = len(st.session_state.companies_loaded or [])
            st.markdown(
                f'<span class="pill ok"><span class="dot"></span>Connected · {n} companies</span>',
                unsafe_allow_html=True,
            )
        st.caption(f"`{host}`")

        if st.button("🔄 Refresh from Tally", use_container_width=True):
            load_companies_into_session()
            st.rerun()

        st.divider()

        st.markdown('<div class="section-eyebrow">Companies to sync</div>', unsafe_allow_html=True)
        loaded = st.session_state.companies_loaded or []

        # Initialize each checkbox to True on first sight (default = all selected).
        for c in loaded:
            key = f"co::{c}"
            if key not in st.session_state:
                st.session_state[key] = True

        if loaded:
            btn_cols = st.columns(2)
            with btn_cols[0]:
                if st.button("Select all", use_container_width=True, key="co_all_btn"):
                    for c in loaded:
                        st.session_state[f"co::{c}"] = True
                    st.rerun()
            with btn_cols[1]:
                if st.button("Clear", use_container_width=True, key="co_clear_btn"):
                    for c in loaded:
                        st.session_state[f"co::{c}"] = False
                    st.rerun()

        for c in loaded:
            st.checkbox(c, key=f"co::{c}")

        selected = [c for c in loaded if st.session_state.get(f"co::{c}", False)]
        st.caption(f"{len(selected)} of {len(loaded)} selected")

        st.divider()
        st.caption("Run only one dashboard instance at a time.")

    return selected


# ------------------------------------------------------------------------------------ setup view

def render_setup_view(state: dict, selected_companies: list[str]) -> None:
    today = state_mod.now_ist().date()
    fy_start = fy_start_for(today)

    # Header
    st.markdown(
        f"""
        <div style="display:flex;align-items:flex-end;justify-content:space-between;margin-bottom:1.2rem;">
          <div>
            <div class="section-eyebrow">Dashboard</div>
            <h1 style="margin:0;font-size:1.9rem;">What do you want to sync?</h1>
            <div class="status-sub">Pick masters and date ranges, then run them all in one go.</div>
          </div>
          <div style="text-align:right;color:{MUTED};font-size:0.85rem;">
            {today.strftime('%A, %d %b %Y')}
          </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    if not selected_companies:
        st.info("Pick at least one company from the sidebar to begin.")
        return

    # Global date-range control — applied to every selected date-range master.
    with st.container(border=True):
        st.markdown('<div class="section-eyebrow">Date range (applies to all date-range masters)</div>', unsafe_allow_html=True)
        global_mode = st.radio(
            "Mode",
            options=["From last pending", "Specific period"],
            horizontal=True,
            key="global_mode",
            label_visibility="collapsed",
        )
        if global_mode == "Specific period":
            dcols = st.columns(2)
            with dcols[0]:
                global_from = st.date_input("From", value=fy_start, key="global_from", format="DD/MM/YYYY")
            with dcols[1]:
                global_to = st.date_input("To", value=today, key="global_to", format="DD/MM/YYYY")
        else:
            global_from = global_to = None
            st.caption(f"From the day after each company's last sync (fallback {fmt_dmy(fy_start)}) → today ({fmt_dmy(today)}). Snapshot masters ignore dates.")

    st.markdown("&nbsp;", unsafe_allow_html=True)

    plans: list[MasterPlan] = []
    for mkey in UI_ORDER:
        m = MASTERS[mkey]
        with st.container(border=True):
            top = st.columns([0.45, 0.30, 0.25])
            with top[0]:
                checked = st.checkbox(
                    " ",
                    key=f"sel_{mkey}",
                    value=st.session_state.master_selected.get(mkey, False),
                    label_visibility="collapsed",
                )
                st.session_state.master_selected[mkey] = checked
                st.markdown(
                    f'<div style="margin-top:-1.6rem;margin-left:1.85rem;">'
                    f'<div class="master-title">{m.label}</div>'
                    f'<div class="master-sub">{_master_blurb(mkey)}</div>'
                    f'</div>',
                    unsafe_allow_html=True,
                )
            with top[1]:
                st.markdown('<div class="section-eyebrow">Last synced</div>', unsafe_allow_html=True)
                st.markdown(watermark_html(state, mkey, selected_companies), unsafe_allow_html=True)
            with top[2]:
                st.markdown('<div class="section-eyebrow">Type</div>', unsafe_allow_html=True)
                if m.date_range:
                    st.markdown('<span class="pill muted">Date range</span>', unsafe_allow_html=True)
                else:
                    st.markdown('<span class="pill muted">Snapshot</span>', unsafe_allow_html=True)

            if checked and m.date_range:
                if global_mode == "Specific period":
                    plans.append(MasterPlan(
                        master=mkey, mode="specific",
                        from_date=fmt_dmy(global_from), to_date=fmt_dmy(global_to),
                        reconcile=st.session_state.get("reconcile_mode", False),
                    ))
                else:
                    plans.append(MasterPlan(
                        master=mkey, mode="last_pending",
                        reconcile=st.session_state.get("reconcile_mode", False),
                    ))
            elif checked and not m.date_range:
                plans.append(MasterPlan(master=mkey, mode="snapshot"))

    # Reconcile mode toggle
    st.markdown("&nbsp;", unsafe_allow_html=True)
    with st.container(border=True):
        reconcile_mode = st.toggle(
            "Full reconcile mode — delete rows absent from Tally",
            value=False,
            key="reconcile_mode",
        )
        if reconcile_mode:
            st.warning(
                "Rows within the synced date range that are **not found in Tally** will be "
                "**permanently deleted** from the sheet. Use only when you trust the Tally data "
                "for the selected period is complete.",
                icon="⚠️",
            )
        else:
            st.caption("Default: append-only. Enable reconcile to also remove rows absent from Tally.")

    # Auto push-to-dashboard toggle (P4): refresh the live dashboard right after a sync.
    with st.container(border=True):
        auto_push = st.toggle(
            "Push to dashboard after fetch",
            value=True,
            key="auto_push_dashboard",
        )
        if auto_push:
            st.caption("After a successful sync that changed any rows, automatically run "
                       "`process_data.py` → Supabase so the live dashboard reflects the new data. "
                       "Skipped if a step errored or nothing changed.")
        else:
            st.caption("The sheets will update, but the live dashboard won't refresh until you run "
                       "`process_data.py` (or the Reconcile & Heal tab's push) manually.")

    # Sticky-feeling action bar
    st.markdown("&nbsp;", unsafe_allow_html=True)
    st.divider()

    can_sync = bool(plans) and bool(selected_companies)
    preview = build_steps(state, plans, selected_companies, today=today) if can_sync else []
    if can_sync and not preview:
        st.info("Everything selected is already up to date — nothing to sync.")
        can_sync = False

    cols = st.columns([3, 2])
    with cols[0]:
        if preview:
            est = estimate_total_seconds(state, preview)
            st.markdown(
                f"**{len(preview)} steps** · {len(plans)} master(s) × {len(selected_companies)} company(ies) "
                f"· est. **{fmt_seconds(est)}**"
            )
        else:
            st.markdown(":grey[Select at least one master to enable sync.]")
    with cols[1]:
        if st.button("▶ Sync Data", type="primary", disabled=not can_sync, use_container_width=True):
            steps = build_steps(state, plans, selected_companies, today=today)
            if steps:
                runner = Runner(state, steps, auto_process_data=st.session_state.get("auto_push_dashboard", True))
                st.session_state.steps = steps
                st.session_state.runner = runner
                st.session_state.run_total_estimate = estimate_total_seconds(state, steps)
                st.session_state.run_started_at = time.time()
                runner.start()
                st.rerun()


def _master_blurb(mkey: str) -> str:
    return {
        "sales":             "Sales vouchers → Sales sheet + Outstanding Register",
        "salescreditnote":   "Credit notes & sales returns → outstanding sheet",
        "salesdebitnote":    "Debit notes against debtors → outstanding sheet",
        "salesjournal":      "Journals against debtors / inter-branch → outstanding sheet",
        "bankreceipt":       "Bank/cash receipts → outstanding sheet",
        "chequereturn":      "Dishonored cheque entries → Chq Return sheet",
        "credit_limit":      "Debtor master · credit period, limit, openings",
        "ledger_master":     "All ledgers snapshot → Ledger Master sheet",
        "stock_item_master": "All stock items snapshot → Stock Item Master sheet",
    }[mkey]


# ------------------------------------------------------------------------------------ running view

def render_running_view(state: dict) -> None:
    runner: Runner = st.session_state.runner
    steps = st.session_state.steps
    runner.drain_events()

    total = len(steps)
    done = sum(1 for s in steps if s.status in ("done", "error", "skipped"))
    running_step = next((s for s in steps if s.status == "running"), None)
    error_count = sum(1 for s in steps if s.status == "error")
    skipped_count = sum(1 for s in steps if s.status == "skipped")
    is_running = runner.is_running()

    # State-aware header (this is the bug fix — header reflects actual phase)
    if is_running:
        title_class, title_icon, title_text = "running", "⏳", "Syncing…"
        sub = _step_caption(running_step) if running_step else "Finalizing the last step…"
    elif error_count and skipped_count:
        title_class, title_icon, title_text = "warn", "⚠", f"Completed with {error_count} error(s) and {skipped_count} skipped"
        sub = "Review the errors below. The sheet has only the rows that pushed successfully."
    elif error_count:
        title_class, title_icon, title_text = "err", "✕", f"Completed with {error_count} error(s)"
        sub = "Review the errors below. The sheet has only the rows that pushed successfully."
    elif skipped_count:
        title_class, title_icon, title_text = "warn", "⊘", "Sync cancelled"
        sub = f"{skipped_count} step(s) were skipped. Already-pushed rows remain on the sheet."
    else:
        title_class, title_icon, title_text = "complete", "✓", "Sync complete"
        sub = f"All {total} steps finished successfully."

    st.markdown(
        f'<div class="status-header {title_class}">{title_icon} {title_text}</div>'
        f'<div class="status-sub">{sub}</div>',
        unsafe_allow_html=True,
    )

    # Live ETA
    remaining_est = 0.0
    for s in steps:
        if s.status == "pending":
            remaining_est += state_mod.estimate_step_seconds(state, s.master, s.phase, s.company)
        elif s.status == "running" and s.started_at is not None:
            this_est = state_mod.estimate_step_seconds(state, s.master, s.phase, s.company)
            elapsed = time.time() - s.started_at
            remaining_est += max(this_est - elapsed, 5)

    elapsed_total = time.time() - (st.session_state.run_started_at or time.time())
    pct = done / total if total else 1.0

    cols = st.columns(4)
    cols[0].metric("Progress", f"{done} / {total}", f"{pct*100:.0f}%")
    cols[1].metric("Elapsed", fmt_seconds(elapsed_total))
    cols[2].metric("ETA", fmt_seconds(remaining_est) if is_running else "—")
    cols[3].metric("Total" if not is_running else "Total est.", fmt_seconds(elapsed_total + (remaining_est if is_running else 0)))

    bar_caption = _step_caption(running_step) if running_step else ("Done" if not is_running else "Finalizing…")
    st.progress(pct, text=bar_caption)

    # Summary card (only when complete)
    if not is_running:
        _render_summary(steps)

    # Live log + step list
    with st.expander("Live log", expanded=is_running):
        st.code("\n".join(runner.log_tail(40)) or "(no output yet)", language="text")

    with st.expander(f"Step list ({total})", expanded=False):
        for i, s in enumerate(steps):
            icon_class = {
                "pending": ("·", "icon-pend"),
                "running": ("◐", "icon-run"),
                "done":    ("✓", "icon-done"),
                "error":   ("✕", "icon-err"),
                "skipped": ("⊘", "icon-skip"),
            }[s.status]
            chunk_part = f" <span style='color:{MUTED}'>[{s.chunk_label}]</span>" if s.chunk_label and s.chunk_label != "snapshot" else ""
            extras = f" <span style='color:{MUTED}'>({s.elapsed:.1f}s)</span>" if s.status in ("done", "error") else ""
            err_part = f" <span style='color:{ERR}'>— {s.error}</span>" if s.error else ""
            # Hub steps (auto process_data) have master='' and no company/phase context.
            tail = f" · {s.company}{chunk_part} · {s.phase}" if s.step_type == "tally" else ""
            st.markdown(
                f'<div class="step-row">'
                f'<span class="step-icon {icon_class[1]}">{icon_class[0]}</span>'
                f'<code>{i+1:02d}</code> '
                f'<b>{step_label(s)}</b>{tail}{extras}{err_part}'
                f'</div>',
                unsafe_allow_html=True,
            )

    # Action row
    st.divider()
    if is_running:
        cols = st.columns([3, 1])
        with cols[1]:
            if st.button("⛔ Cancel sync", type="secondary", use_container_width=True):
                runner.cancel()
                st.warning("Cancel requested — finishing current step then stopping…")
        # Re-render every second while running so progress + ETA update
        time.sleep(1.0)
        st.rerun()
    else:
        cols = st.columns([3, 1])
        with cols[1]:
            if st.button("← Back to dashboard", type="primary", use_container_width=True):
                st.session_state.runner = None
                st.session_state.steps = None
                st.rerun()


def _step_caption(s) -> str:
    if s is None:
        return ""
    if s.step_type != "tally":
        return step_label(s)  # hub step (e.g. auto process_data) — no company/phase context
    m_label = MASTERS[s.master].label
    chunk_part = f" — {s.chunk_label}" if s.chunk_label and s.chunk_label != "snapshot" else ""
    phase_label = "Fetching" if s.phase == "fetch" else "Pushing"
    return f"{m_label} → {s.company}{chunk_part} → {phase_label}"


def _render_summary(steps) -> None:
    by_master: dict[str, dict[str, Any]] = {}
    errors: list[str] = []
    hub_steps = [s for s in steps if s.step_type != "tally"]
    for s in steps:
        if s.step_type != "tally":
            continue  # hub steps (auto process_data) shown separately below
        agg = by_master.setdefault(s.master, {"fetched": 0, "appended": 0, "skipped": 0, "deleted": 0, "errors": 0, "details": None})
        if s.status == "error":
            agg["errors"] += 1
            errors.append(f"{MASTERS[s.master].label} → {s.company} ({s.phase}): {s.error}")
        elif s.phase == "push" and s.summary:
            agg["fetched"] += int(s.summary.get("fetched", 0) or 0)
            agg["appended"] += int(s.summary.get("appended", 0) or 0)
            agg["skipped"] += int(s.summary.get("skipped", 0) or 0)
            agg["deleted"] += int(s.summary.get("deleted", 0) or 0)
            # Accumulate nested details (Sales Outstanding Register)
            d = s.summary.get("details")
            if isinstance(d, dict) and not d.get("skipped_legacy") and not d.get("skipped_no_env"):
                if agg["details"] is None:
                    agg["details"] = {"fetched": 0, "appended": 0, "skipped": 0, "deleted": 0}
                agg["details"]["fetched"] += int(d.get("fetched", 0) or 0)
                agg["details"]["appended"] += int(d.get("appended", 0) or 0)
                agg["details"]["skipped"] += int(d.get("skipped", 0) or 0)
                agg["details"]["deleted"] += int(d.get("deleted", 0) or 0)

    if not by_master:
        return

    st.markdown('<div class="section-eyebrow" style="margin-top:0.5rem;">Summary</div>', unsafe_allow_html=True)
    cols = st.columns(min(len(by_master), 5))
    for i, (mkey, agg) in enumerate(by_master.items()):
        with cols[i % len(cols)]:
            with st.container(border=True):
                st.markdown(f'<div class="master-title">{MASTERS[mkey].label}</div>', unsafe_allow_html=True)
                tone = "ok" if not agg["errors"] else "err"
                badge = "✓ ok" if not agg["errors"] else f"✕ {agg['errors']} err"
                st.markdown(
                    f'<span class="pill {tone}"><span class="dot"></span>{badge}</span>',
                    unsafe_allow_html=True,
                )
                deleted_part = f" · Deleted <b style='color:#DC2626'>{agg['deleted']}</b>" if agg["deleted"] else ""
                st.markdown(
                    f'<div style="margin-top:0.5rem;font-size:0.85rem;color:{MUTED};">'
                    f"Fetched <b style='color:#1C1917'>{agg['fetched']}</b> · "
                    f"Appended <b style='color:#1C1917'>{agg['appended']}</b> · "
                    f"Skipped <b style='color:#1C1917'>{agg['skipped']}</b>"
                    f"{deleted_part}"
                    f"</div>",
                    unsafe_allow_html=True,
                )
                # Show Sales Outstanding Register stats when present
                if agg["details"] is not None:
                    d = agg["details"]
                    d_deleted_part = f" · Deleted <b style='color:#DC2626'>{d['deleted']}</b>" if d.get("deleted") else ""
                    st.markdown(
                        f'<div style="margin-top:0.35rem;font-size:0.80rem;color:{MUTED};border-top:1px solid #E7E5E4;padding-top:0.35rem;">'
                        f"<b>Outstanding Register</b> — "
                        f"Fetched <b style='color:#1C1917'>{d['fetched']}</b> · "
                        f"Appended <b style='color:#1C1917'>{d['appended']}</b> · "
                        f"Skipped <b style='color:#1C1917'>{d['skipped']}</b>"
                        f"{d_deleted_part}"
                        f"</div>",
                        unsafe_allow_html=True,
                    )

    if errors:
        with st.expander(f"❌ Errors ({len(errors)})", expanded=True):
            for e in errors:
                st.write(f"- {e}")

    # Auto process_data (P4) result line — the dashboard push that ran after the sync.
    for h in hub_steps:
        if h.step_type == "hub_process_data":
            if h.status == "done":
                st.success("↻ Pushed to the live dashboard (process_data → Supabase).", icon="✅")
            elif h.status == "error":
                st.error(f"↻ Auto-push to dashboard failed: {h.error}. Run process_data manually.", icon="⚠️")


# ------------------------------------------------------------------------------------ cancellation audit

_AUDIT_MASTER_LABELS = {
    "sales":           "Sales",
    "salescreditnote": "Credit Note",
    "salesdebitnote":  "Debit Note",
    "salesjournal":    "Journal",
    "bankreceipt":     "Bank Receipt",
    "chequereturn":    "Cheque Return",
}


def render_audit_tab(selected_companies: list[str]) -> None:
    today = state_mod.now_ist().date()
    fy_start = fy_start_for(today)

    st.markdown(
        f"""
        <div style="margin-bottom:1.2rem;">
          <div class="section-eyebrow">Cancellation Audit</div>
          <h1 style="margin:0;font-size:1.9rem;">Cancelled / Deleted Voucher Check</h1>
          <div class="status-sub">
            Fetches active vouchers from Tally and flags any voucher that is in the
            Google Sheet but no longer exists in Tally — indicating it was cancelled
            or deleted after the last sync.
          </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    if not selected_companies:
        st.info("Pick at least one company from the sidebar to begin.")
        return

    audit_runner: AuditRunner | None = st.session_state.audit_runner

    # ---- poll running audit -------------------------------------------------
    if audit_runner is not None and audit_runner.is_running():
        events = audit_runner.drain_events()
        for ev in events:
            kind = ev.get("kind", "")
            if kind == "progress":
                st.session_state.audit_log.append(
                    f"  Fetching {_AUDIT_MASTER_LABELS.get(ev['master'], ev['master'])} · "
                    f"{ev['company'][:30]} · {ev['chunk']}  [{ev['step']}/{ev['total']}]"
                )
            elif kind == "reading_sheet":
                st.session_state.audit_log.append(
                    f"  Reading sheet for {_AUDIT_MASTER_LABELS.get(ev['master'], ev['master'])}…"
                )
            elif kind == "master_done":
                st.session_state.audit_log.append(
                    f"  ✓ {_AUDIT_MASTER_LABELS.get(ev['master'], ev['master'])}: "
                    f"sheet={ev['sheet_rows']} vouchers · tally={ev['tally_rows']} · "
                    f"flagged={ev['flagged']}"
                )
            elif kind in ("warn", "error"):
                st.session_state.audit_log.append(
                    f"  ⚠ {_AUDIT_MASTER_LABELS.get(ev.get('master',''), ev.get('master',''))}: {ev['msg']}"
                )

        # Show live progress
        total_steps = None
        step = None
        for ev in events:
            if ev.get("kind") == "progress":
                total_steps = ev.get("total")
                step = ev.get("step")
        if total_steps and step:
            st.progress(step / total_steps, text=st.session_state.audit_log[-1] if st.session_state.audit_log else "Running…")
        else:
            st.progress(0.0, text="Starting…")

        with st.expander("Live log", expanded=True):
            st.code("\n".join(st.session_state.audit_log[-40:]), language="text")

        st.info("Audit in progress — this page refreshes automatically.")
        time.sleep(1.0)
        st.rerun()
        return

    # ---- audit just finished ------------------------------------------------
    if audit_runner is not None and not audit_runner.is_running():
        events = audit_runner.drain_events()
        for ev in events:
            kind = ev.get("kind", "")
            if kind == "master_done":
                st.session_state.audit_log.append(
                    f"  ✓ {_AUDIT_MASTER_LABELS.get(ev['master'], ev['master'])}: "
                    f"sheet={ev['sheet_rows']} · tally={ev['tally_rows']} · flagged={ev['flagged']}"
                )
            elif kind in ("warn", "error"):
                st.session_state.audit_log.append(
                    f"  ⚠ {_AUDIT_MASTER_LABELS.get(ev.get('master', ''), ev.get('master', ''))}: {ev['msg']}"
                )
        if audit_runner.error and not any("⚠" in line for line in st.session_state.audit_log):
            st.session_state.audit_log.append(f"  ⚠ Audit failed: {audit_runner.error}")
        st.session_state.audit_results = audit_runner.results
        st.session_state.audit_runner  = None

    # ---- setup form ---------------------------------------------------------
    with st.container(border=True):
        st.markdown('<div class="section-eyebrow">Audit date range</div>', unsafe_allow_html=True)
        dcols = st.columns(2)
        with dcols[0]:
            audit_from = st.date_input("From", value=fy_start, key="audit_from", format="DD/MM/YYYY")
        with dcols[1]:
            audit_to = st.date_input("To", value=today, key="audit_to", format="DD/MM/YYYY")

    with st.container(border=True):
        st.markdown('<div class="section-eyebrow">Masters to check</div>', unsafe_allow_html=True)
        audit_master_cols = st.columns(3)
        selected_masters: list[str] = []
        for i, (mkey, mlabel) in enumerate(_AUDIT_MASTER_LABELS.items()):
            with audit_master_cols[i % 3]:
                if st.checkbox(mlabel, value=True, key=f"audit_master_{mkey}"):
                    selected_masters.append(mkey)

    st.markdown("&nbsp;", unsafe_allow_html=True)

    chunk_count = len(_month_chunks_ui(audit_from, audit_to))
    step_count  = len(selected_masters) * len(selected_companies) * chunk_count
    st.markdown(
        f"**{step_count} fetch steps** · {len(selected_masters)} master(s) × "
        f"{len(selected_companies)} company(ies) × {chunk_count} month(s). "
        f"Estimated **{step_count * 5 // 60}–{step_count * 8 // 60} min**."
    )

    if st.button("🔍 Run Cancellation Audit", type="primary",
                 disabled=not selected_masters or not selected_companies):
        st.session_state.audit_results = None
        st.session_state.audit_log     = ["Starting cancellation audit…"]
        runner = AuditRunner(
            companies  = selected_companies,
            masters    = selected_masters,
            from_date  = audit_from,
            to_date    = audit_to,
        )
        st.session_state.audit_runner = runner
        runner.start()
        st.rerun()

    # ---- results ------------------------------------------------------------
    results: list[dict] | None = st.session_state.audit_results
    if results is None:
        return

    st.divider()

    if st.session_state.audit_log:
        with st.expander("Audit log", expanded=False):
            st.code("\n".join(st.session_state.audit_log), language="text")

    if not results:
        st.success("✓ No cancelled or deleted vouchers found — every voucher in the sheets is still active in Tally.")
        return

    # Group by master
    by_master: dict[str, list[dict]] = {}
    for row in results:
        by_master.setdefault(row["master"], []).append(row)

    total_flagged = len(results)
    st.error(f"⚠ {total_flagged} voucher(s) flagged across {len(by_master)} master(s) — present in sheet but absent from Tally.")
    st.caption("These vouchers may have been cancelled or deleted in Tally after the last sync. Review each one and delete the corresponding rows from the sheet if confirmed cancelled.")

    for master, rows in by_master.items():
        label = _AUDIT_MASTER_LABELS.get(master, master)
        with st.expander(f"**{label}** — {len(rows)} flagged", expanded=True):
            import pandas as pd
            df = pd.DataFrame(rows)[["company", "location", "voucher_no", "date", "particulars", "amount", "remark"]]
            df.columns = ["Company", "Location", "Voucher No.", "Date", "Particulars", "Amount", "Remark"]
            st.dataframe(df, use_container_width=True, hide_index=True)

    if st.button("↺ Clear results", key="audit_clear"):
        st.session_state.audit_results = None
        st.session_state.audit_log     = []
        st.rerun()


def _month_chunks_ui(start: date, end: date) -> list[tuple[date, date]]:
    chunks: list[tuple[date, date]] = []
    cur = start
    while cur <= end:
        month_end = (date(cur.year, cur.month + 1, 1) - timedelta(days=1)) if cur.month < 12 else date(cur.year, 12, 31)
        chunks.append((cur, min(month_end, end)))
        cur = month_end + timedelta(days=1)
    return chunks


# ------------------------------------------------------------------------------------ reconcile & heal (P6)

def _hub_paths(as_of: date) -> tuple[Path, Path, str]:
    """(tally_cache, ledger_map, ddmm) for an as-of, by the Hub's date-tagged convention."""
    ddmm = as_of.strftime("%d%m")
    cache = HUB_ROOT / "scripts" / f"tally_cache_{ddmm}_ledger.json"
    ledger_map = HUB_ROOT / ".tmp" / "stage0" / f"ledger_master_{ddmm}.json"
    return cache, ledger_map, ddmm


def _prior_cache() -> str | None:
    """The 31-03 fy2526 cache, used by the generic builder for overdue reuse + cross-check."""
    p = HUB_ROOT / "scripts" / "tally_cache_3103_ledger.json"
    return str(p) if p.exists() else None


def _latest_report() -> Path | None:
    reports = sorted((HUB_ROOT / "scripts" / "reports").glob("reconciliation_*.xlsx"), reverse=True)
    return reports[0] if reports else None


def _run_blocking(argv: list[str], cwd: Path, label: str, timeout: int = 1800) -> tuple[int, str, str]:
    """Run a Hub/FETCH subprocess to completion under a spinner; tee tail into recon_log."""
    env = {**os.environ, "PYTHONUTF8": "1"}
    st.session_state.recon_log.append(f"$ {label}")
    with st.spinner(label):
        try:
            proc = subprocess.run(argv, cwd=str(cwd), capture_output=True, text=True, env=env, timeout=timeout)
        except subprocess.TimeoutExpired:
            st.session_state.recon_log.append(f"  ✗ timed out after {timeout}s")
            return 1, "", "timeout"
    out = (proc.stdout or "")
    for ln in out.splitlines()[-12:]:
        st.session_state.recon_log.append("  " + ln)
    if proc.returncode != 0:
        st.session_state.recon_log.append(f"  ✗ exit {proc.returncode}: {(proc.stderr or '')[-300:]}")
    return proc.returncode, out, (proc.stderr or "")


def _parse_last_json(stdout: str) -> dict | None:
    for ln in reversed(stdout.splitlines()):
        ln = ln.strip()
        if ln.startswith("{") and ln.endswith("}"):
            try:
                return json.loads(ln)
            except json.JSONDecodeError:
                continue
    return None


def _reconcile_and_plan(as_of: date) -> bool:
    """Read-only reconcile (reuse cache) → self_heal --json plan → store routed rows. Returns ok."""
    cache, ledger_map, _ = _hub_paths(as_of)
    as_of_s = as_of.strftime("%d-%m-%Y")
    if not cache.exists() or not ledger_map.exists():
        st.session_state.recon_log.append(
            f"  ✗ no cache/ledger-map for {as_of_s} — rebuild the Tally cache first.")
        return False
    rc, _, _ = _run_blocking(reconcile_step(as_of_s, str(cache), str(ledger_map)).argv,
                             HUB_ROOT, f"Reconciling as-of {as_of_s} (read-only, reusing cache)…")
    if rc != 0:
        return False
    report = _latest_report()
    if not report:
        st.session_state.recon_log.append("  ✗ reconcile produced no report.")
        return False
    plan_argv = heal_step(str(report), as_of_s, str(cache), _prior_cache() or str(cache),
                          "", execute=False).argv
    rc2, out2, _ = _run_blocking(plan_argv, HUB_ROOT, "Classifying gaps (Channel A / B)…")
    plan = _parse_last_json(out2)
    if rc2 != 0 or not plan:
        st.session_state.recon_log.append("  ✗ could not parse the heal plan.")
        return False
    st.session_state.recon_rows = plan.get("rows", [])
    st.session_state.recon_asof = as_of_s
    st.session_state.recon_report = str(report)
    st.session_state.recon_heal_sel = {r["ledger_id"]: True for r in st.session_state.recon_rows if r.get("healable")}
    st.session_state.recon_log.append(
        f"  ✓ {len(st.session_state.recon_rows)} flagged rows classified.")
    return True


def _rebuild_cache_then_reconcile(as_of: date, companies: list[str]) -> bool:
    """Tally: fetch ledger-master (all loaded co) → build cache → reconcile + plan. Needs Tally up."""
    cache, ledger_map, _ = _hub_paths(as_of)
    as_of_s = as_of.strftime("%d-%m-%Y")
    ledger_map.parent.mkdir(parents=True, exist_ok=True)
    py = sys.executable or "python"
    fetch_tool = SKILLS_ROOT / "tally-ledger-master-sync" / "tools" / "fetch_tally_ledger_master.py"
    rc, _, _ = _run_blocking(
        [py, str(fetch_tool), "--from", "01-04-2025", "--to", as_of_s, "--output", str(ledger_map)],
        PROJECT_ROOT, f"Fetching Tally ledger master (all loaded companies) as-of {as_of_s}…", timeout=2400)
    if rc != 0 or not ledger_map.exists():
        st.session_state.recon_log.append("  ✗ ledger-master fetch failed — is Tally up? Aborting rebuild.")
        return False
    rc2, _, _ = _run_blocking(
        cache_build_step(as_of_s, str(ledger_map), str(cache), _prior_cache()).argv,
        HUB_ROOT, f"Building Tally cache for {as_of_s}…")
    if rc2 != 0:
        return False
    return _reconcile_and_plan(as_of)


def _heal_selected(as_of: date, selected_ids: list[str]) -> bool:
    """Execute the heal for the selected GUIDs (engine fetches + heals + process_data), then re-reconcile."""
    cache, ledger_map, _ = _hub_paths(as_of)
    as_of_s = as_of.strftime("%d-%m-%Y")
    report = st.session_state.recon_report or (str(_latest_report()) if _latest_report() else None)
    if not report:
        st.session_state.recon_log.append("  ✗ no report to heal from.")
        return False
    argv = heal_step(report, as_of_s, str(cache), _prior_cache() or str(cache),
                     ",".join(selected_ids), execute=True).argv
    rc, out, _ = _run_blocking(argv, HUB_ROOT,
                               f"Healing {len(selected_ids)} party(ies) + process_data…", timeout=3600)
    res = _parse_last_json(out)
    if rc != 0:
        st.session_state.recon_log.append("  ✗ heal failed — see log; backups are in <FETCH>/backups/heal_*.")
        return False
    if res:
        st.session_state.recon_log.append(
            f"  ✓ healed {len(res.get('healed', []))}; process_data={res.get('process_data')}.")
    # Auto re-reconcile to confirm the gaps cleared (read-only, reuses the same cache — heal changed
    # the SHEETS, not Tally, so the cache stays valid).
    st.session_state.recon_log.append("  ↻ Re-reconciling to confirm…")
    return _reconcile_and_plan(as_of)


def render_reconcile_tab(state: dict, selected_companies: list[str]) -> None:
    today = state_mod.now_ist().date()
    st.markdown(
        f"""
        <div style="margin-bottom:1rem;">
          <div class="section-eyebrow">Reconcile &amp; Heal</div>
          <h1 style="margin:0;font-size:1.7rem;">Find &amp; fix Tally ↔ Sheets ↔ dashboard gaps</h1>
          <div class="status-sub">Pick an as-of date, run the reconcile, then heal the flagged parties
          surgically (GUID-keyed). Outstanding is a balance, so the period is always
          <b>FY-start → as-of</b>.</div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    with st.container(border=True):
        c = st.columns([0.4, 0.6])
        with c[0]:
            as_of = st.date_input("As-of date", value=today, key="recon_asof_input", format="DD/MM/YYYY")
        cache, ledger_map, ddmm = _hub_paths(as_of)
        with c[1]:
            if cache.exists() and ledger_map.exists():
                mt = datetime.fromtimestamp(cache.stat().st_mtime).strftime("%d %b %H:%M")
                fresh = datetime.fromtimestamp(cache.stat().st_mtime).date() == today
                st.markdown(
                    f"**Tally cache:** `{cache.name}` · built {mt} "
                    f"{'· :green[today]' if fresh else '· :orange[not today — consider rebuild]'}")
            else:
                st.markdown(":orange[**No Tally cache for this date.**] Rebuild it first (needs Tally up).")

        bcols = st.columns([0.34, 0.34, 0.32])
        with bcols[0]:
            if st.button("▶ Run Reconcile", type="primary", use_container_width=True,
                         disabled=not (cache.exists() and ledger_map.exists())):
                if _reconcile_and_plan(as_of):
                    st.rerun()
        with bcols[1]:
            rebuild = st.button("🔄 Rebuild Tally cache + reconcile", use_container_width=True,
                                disabled=not selected_companies,
                                help="Fetches the Tally ledger master (one call, all loaded companies) "
                                     "then rebuilds the cache. Needs Tally up; run ONE Tally job at a time.")
            if rebuild:
                if _rebuild_cache_then_reconcile(as_of, selected_companies):
                    st.rerun()
        with bcols[2]:
            st.caption("Run only when no sync is running (one Tally job at a time).")

    rows = st.session_state.recon_rows
    if rows is None:
        st.info("Run a reconcile to see the gap diagnosis here.")
        if st.session_state.recon_log:
            with st.expander("Log", expanded=False):
                st.code("\n".join(st.session_state.recon_log[-40:]), language="text")
        return

    heal_rows = [r for r in rows if r.get("healable")]
    show_rows = [r for r in rows if r.get("has_sync") and not r.get("healable")]
    pipe_rows = [r for r in rows if r.get("has_pipe")]

    k = st.columns(3)
    k[0].metric("Channel A · auto-healable", len(heal_rows))
    k[1].metric("Channel A · shown (manual)", len(show_rows))
    k[2].metric("Channel B · pipeline", len(pipe_rows))

    def _inr(v):
        return "—" if v is None else f"₹{v:,.0f}"

    # ── Channel A — auto-healable (per-row select) ──────────────────────────────
    st.markdown('<div class="section-eyebrow" style="margin-top:0.8rem;">Channel A — auto-healable (FY2627 flow)</div>', unsafe_allow_html=True)
    if heal_rows:
        hc = st.columns([0.12, 0.30, 0.16, 0.16, 0.26])
        hc[0].markdown("**Heal**"); hc[1].markdown("**Customer**"); hc[2].markdown("**Co/Loc**")
        hc[3].markdown("**Δ Sync**"); hc[4].markdown("**Ledger ID**")
        for r in heal_rows:
            lid = r["ledger_id"]
            rc_ = st.columns([0.12, 0.30, 0.16, 0.16, 0.26])
            sel = rc_[0].checkbox(" ", value=st.session_state.recon_heal_sel.get(lid, True),
                                  key=f"heal_{lid}", label_visibility="collapsed")
            st.session_state.recon_heal_sel[lid] = sel
            rc_[1].write(r["name"][:38])
            rc_[2].write(f"{r['company']}/{r['location']}")
            rc_[3].write(_inr(r.get("delta_sync")))
            rc_[4].code(lid[-12:])
        selected_ids = [r["ledger_id"] for r in heal_rows if st.session_state.recon_heal_sel.get(r["ledger_id"])]
        hb = st.columns([0.6, 0.4])
        with hb[1]:
            if st.button(f"✅ Heal Selected ({len(selected_ids)})", type="primary",
                         use_container_width=True, disabled=not selected_ids):
                if _heal_selected(as_of, selected_ids):
                    st.rerun()
        hb[0].caption("Heal re-fetches each party's FY-start→as-of slice from Tally, GUID-deletes/re-inserts "
                      "only their rows in the blamed sheets, backs up first, then runs one process_data.")
    else:
        st.caption("No auto-healable (FY2627-flow) sync gaps.")

    # ── Channel A — shown with reason ───────────────────────────────────────────
    if show_rows:
        with st.expander(f"Channel A — shown with reason, NOT auto-healed ({len(show_rows)})", expanded=False):
            for r in show_rows:
                st.markdown(f"• **{r['name'][:40]}** ({r['company']}/{r['location']}) "
                            f"Δsync {_inr(r.get('delta_sync'))} — `{r.get('bucket')}` — {r.get('reason')}")

    # ── Channel B — pipeline ────────────────────────────────────────────────────
    st.markdown('<div class="section-eyebrow" style="margin-top:0.8rem;">Channel B — pipeline (clears on process_data)</div>', unsafe_allow_html=True)
    if pipe_rows:
        for r in pipe_rows:
            st.markdown(f"→ **{r['name'][:40]}** ({r['company']}/{r['location']}) Δpipe {_inr(r.get('delta_pipe'))}")
        if not heal_rows:
            if st.button("↻ Push to dashboard (process_data only)", use_container_width=False):
                # No sheet heal needed — just refresh Supabase from the current sheets.
                rc, _, _ = _run_blocking(
                    [sys.executable, str(HUB_ROOT / "scripts" / "process_data.py")],
                    HUB_ROOT, "Running process_data → Supabase…", timeout=1800)
                if rc == 0 and _reconcile_and_plan(as_of):
                    st.rerun()
    else:
        st.caption("No pipeline gaps.")

    with st.expander("Log", expanded=False):
        st.code("\n".join(st.session_state.recon_log[-60:]) or "(empty)", language="text")


# ------------------------------------------------------------------------------------ main

def main() -> None:
    inject_css()
    init_session()
    state = state_mod.load_state()

    # Sync running view takes the full screen — no tabs
    if st.session_state.runner is not None:
        render_running_view(state)
        return

    selected_companies = render_sidebar()

    tab_sync, tab_recon, tab_audit = st.tabs(["Sync", "Reconcile & Heal", "Cancellation Audit"])

    with tab_sync:
        render_setup_view(state, selected_companies)

    with tab_recon:
        render_reconcile_tab(state, selected_companies)

    with tab_audit:
        render_audit_tab(selected_companies)


main()
