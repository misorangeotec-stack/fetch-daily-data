# Tally Sync Dashboard

A Streamlit utility that orchestrates the 6 Tally outstanding-report syncs (Sales, Sales Credit Note, Sales Debit Note, Bank Receipt, Cheque Return, Credit Limit & Opening) from a single screen. No Claude required — anyone on the team can run a sync.

## What it does

1. Lists Tally companies live (you pick which to sync; all selected by default).
2. Lists the 6 masters with their last-synced timestamp; you tick which to sync.
3. For date-range masters, choose **From last pending** (resumes from last `--to`) or **Specific period** (pick dates).
4. Click **Sync Data** — runs each master × company sequentially with a live progress bar, ETA, and per-step log.

State (last sync per master/company + duration history for ETA) is persisted in `sync_state.json` next to this README.

## Launch

### On the Tally machine (host)

After the one-time install:

```bash
pip install -r tools/sync_dashboard/requirements.txt
```

Daily launch: **double-click `run_dashboard.bat` in the project root.** The window prints the local + LAN URLs and stays open while the dashboard runs (Ctrl+C to stop).

Manual equivalent if you prefer the command line:

```bash
streamlit run tools/sync_dashboard/app.py --server.address 0.0.0.0 --server.port 8501
```

Binding to `0.0.0.0` makes the dashboard reachable from any PC on the same LAN/VPN. Without it, only the Tally machine itself can open the page.

Pre-reqs (host only):
- Tally Prime running with HTTP/XML enabled (default `http://localhost:9000`).
- `.env` in project root with `TALLY_HOST` and the `*_SHEET_URL` / `*_SHEET_TAB` vars.
- Google OAuth `credentials.json` + `token.json` already set up (used by push scripts).
- Windows Firewall: allow inbound TCP on port 8501 (one-time prompt the first time you launch with `--server.address 0.0.0.0`, or add a rule manually).

Find the host machine's LAN IP with `ipconfig` (look for "IPv4 Address" under the active adapter, e.g. `192.168.1.42`).

### For teammates (no install required)

Open a browser on a machine on the same network and go to:

```
http://<tally-machine-ip>:8501
```

e.g. `http://192.168.1.42:8501`. No Python, no Tally, no Google credentials needed on the teammate's PC — the dashboard runs entirely on the host.

## Notes

- Date-range syncs are chunked **per calendar month** automatically — Tally's HTTP read times out beyond ~1 month per call.
- Push scripts dedupe against the sheet, so retries / overlapping ranges are safe.
- Run only one dashboard instance at a time (state file is not lock-protected — if two teammates click **Sync Data** at the same time, the watermark file will race).
- Tally Prime must stay open on the host for syncs to work; if the host PC sleeps or Tally is closed, teammates' clicks will fail with `Connection refused`.
- The dashboard has no built-in auth — anyone on the LAN who knows the URL can trigger a sync. Keep it on a trusted network, or front it with a reverse proxy + basic auth if you need access control.
