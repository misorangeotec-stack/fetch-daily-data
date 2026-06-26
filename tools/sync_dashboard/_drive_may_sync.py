"""One-off sequential sync of the 6 transaction masters for May 2026 (01-05 -> 27-05),
all 4 companies, to align the Google Sheets with the 27-May bill-wise snapshot.
Sequential, one company at a time (Tally crash guardrail). Fetch -> push per step.
Resilient: logs each step, continues on error, prints a summary at the end.
"""
from __future__ import annotations
import subprocess, sys, time, json
from pathlib import Path

ROOT = Path(r"d:/Agentic AI Tools/Orange O tec/FETCH DAILY DATA")
SKILLS = ROOT / ".claude" / "skills"
TMP = ROOT / ".tmp"
PY = sys.executable or "python"
FROM, TO = "01-05-2026", "27-05-2026"

COMPANIES = [
    "ORANGE O TEC PRIVATE LIMITED (01-04-25TO31-03-27)",
    "ORANGE O TEC PRIVATE LIMITED-NOIDA-(from 1-Apr-25)",
    "ORANGE O TEC ENTERPRISES PVT LTD - (from 1-Apr-24)",
    "ORANGE O TEC ENTERPRISES PRIVATE LIMITED-NOIDA - (from 1-Apr-25)",
]
# (key, skill_dir, fetch_script, push_script, reconcile_register)
MASTERS = [
    ("sales", "tally-sales-sync-outstanding", "fetch_tally_sales.py", "push_sales_to_sheet.py", True),
    ("creditnote", "tally-salescreditnote-sync-outstanding", "fetch_tally_credit_notes.py", "push_credit_notes_to_sheet.py", False),
    ("debitnote", "tally-salesdebitnote-sync-outstanding", "fetch_tally_debit_notes.py", "push_debit_notes_to_sheet.py", False),
    ("journal", "tally-salesjournal-sync-outstanding", "fetch_tally_journals.py", "push_journals_to_sheet.py", False),
    ("bankreceipt", "tally-bankreceipt-sync-outstanding", "fetch_tally_bankreceipt.py", "push_bankreceipt_to_sheet.py", False),
    ("chequereturn", "tally-chequereturn-sync-outstanding", "fetch_tally_chqreturn.py", "push_chqreturn_to_sheet.py", False),
]

def run(cmd, cwd):
    print(f"  $ {' '.join(str(c) for c in cmd)}", flush=True)
    p = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True)
    last = ""
    for line in (p.stdout or "").splitlines():
        if line.strip().startswith("{") and line.strip().endswith("}"):
            last = line.strip()
    if p.returncode != 0:
        print(f"    ERR rc={p.returncode}: {(p.stderr or p.stdout)[-500:]}", flush=True)
    elif last:
        print(f"    -> {last}", flush=True)
    return p.returncode, last

summary = []
for mkey, sdir, fetch, push, recon in MASTERS:
    tools = SKILLS / sdir / "tools"
    for co in COMPANIES:
        tag = f"{mkey}|{co[:28]}"
        print(f"\n=== {tag} | {FROM}..{TO} ===", flush=True)
        out = TMP / f"_maysync_{mkey}_{abs(hash(co))%100000}.json"
        # cwd = project ROOT: push tools resolve credentials.json/token.json relative to cwd.
        rc, _ = run([PY, str(tools / fetch), "--from", FROM, "--to", TO,
                     "--companies", co, "--output", str(out)], ROOT)
        if rc != 0:
            summary.append((tag, "FETCH_FAIL")); continue
        pcmd = [PY, str(tools / push), "--input", str(out)]
        if recon:
            pcmd += ["--reconcile", "--from", FROM, "--to", TO, "--companies", co]
        rc2, s2 = run(pcmd, ROOT)
        summary.append((tag, "ok" if rc2 == 0 else "PUSH_FAIL"))
        time.sleep(0.3)

print("\n===== SUMMARY =====", flush=True)
for tag, st in summary:
    print(f"  {st:12} {tag}", flush=True)
fails = [t for t, s in summary if s != "ok"]
print(f"\n{len(summary)-len(fails)}/{len(summary)} ok; fails: {fails}", flush=True)
