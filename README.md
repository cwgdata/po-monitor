# PO Monitor

A self-contained Databricks App for **observing and operating Predictive
Optimization (PO)** on Unity Catalog managed Iceberg and Delta tables.

Built for data platform teams running UC managed Iceberg/Delta at scale —
PB-class tables, external writers (EMR Spark/Flink), and the kind of
operational concerns that come with both. PO Monitor surfaces whether PO
is keeping up, where it isn't, and gives you the buttons to do something
about it without leaving the app.

> **Disclaimer.** PO Monitor is provided **as-is**, without warranty of any
> kind. It is **not an official Databricks product** and is not supported
> by Databricks. Review the source and validate behavior in your environment
> before relying on it.

---

## What you get

**A live read on PO health for every table you care about.** Per-table cards
roll up DESC DETAIL metrics, PO run history, MERGE activity, and trend
deltas into a single green / amber / red badge with the reasons spelled out.

**Catalog and schema rollups.** Add a single card that aggregates badge
counts, totals, and top offenders across every managed Iceberg/Delta table
in a catalog or schema — useful when you have hundreds of tables and need
a "where should I look?" view.

**Maintenance buttons.** Trigger OPTIMIZE, VACUUM (LITE or FULL), enable /
disable PO at the table or catalog/schema level, and a "Force PO" stand-in
that submits OPTIMIZE + VACUUM LITE as the user. Every action is audited
to a Unity Catalog table.

**Alerts.** Configure rule-based alerts (VACUUM age, OPTIMIZE failure rate,
MERGE conflict spikes, custom thresholds) with delivery via Slack webhooks
or Databricks email notification destinations. A background loop evaluates
rules on a schedule.

**Schedules.** Cron and trigger-based schedules for OPTIMIZE / VACUUM
operations, persisted in UC and fired by an in-app tick loop.

**On-behalf-of-user authorization.** Every data read and every maintenance
action runs as the logged-in user. Unity Catalog ACLs are enforced
per-request — no service principal bypass for end-user operations.

---

## Quick start

```bash
git clone https://github.com/cwgdata/po-monitor.git
cd po-monitor

# Copy the example, fill in your workspace host + warehouse id
cp scripts/deploy.env.example scripts/.deploy.env
$EDITOR scripts/.deploy.env

./scripts/deploy.sh
```

Open the deployed app from **Compute → Apps** in your workspace, sign in,
and pick a warehouse from the modal that appears on first run. The sidebar
selector lets you browse catalogs and pick tables; rollup cards add via the
**+ rollup** button next to the catalog or schema dropdown.

### Prerequisites

- Databricks CLI v0.260+ ([install guide](https://docs.databricks.com/dev-tools/cli/index.html))
- Node 18+ and npm
- Authenticated against the target workspace (`databricks auth login --host …`)
- A Unity-Catalog-enabled workspace
- A SQL warehouse the app's service principal can use

### Workspace permissions

The user running `deploy.sh` needs:

- `CAN_MANAGE` on the workspace (to create the app)
- `CAN_USE` on the chosen SQL warehouse
- Write access under `/Workspace/Users/<you>/`

End users querying through the app need their own UC privileges plus
`CAN_USE` on a warehouse (the one they pick from the sidebar dropdown).

---

## Features

### Per-table health

Each card shows:

- **Composite badge** — green / amber / red, computed from the full ruleset
  below. Triggered signals are listed under the badge.
- **KPIs** — file count, total size, average file size (with 7-day trend
  arrows), DV count where available.
- **Trend chart** — file count over the last 30 days, derived from PO
  OPTIMIZE runs.
- **Recent operations** — unified view of PO-driven runs (from
  `system.storage.predictive_optimization_operations_history`) and manual
  ops (from `DESCRIBE HISTORY`). Each row indicates source and status.
- **MERGE activity (24h)** — count, conflict rate, and recent failure
  samples from `system.query.history`.
- **Action buttons** — OPTIMIZE, VACUUM LITE/FULL, enable/disable PO,
  Force PO, Schedule.

### Health badge ruleset

| Signal | Threshold (default) | Severity |
|---|---|---|
| OPTIMIZE failure rate | > 30% | Red |
| OPTIMIZE failure rate | ≥ 10% | Amber |
| Days since last VACUUM | > 30 | Red |
| Days since last VACUUM | > 14 | Amber |
| MERGE conflict rate (24h, ≥3 samples) | > 30% | Red |
| MERGE conflict rate (24h, ≥3 samples) | ≥ 10% | Amber |
| Unclustered ratio (bytes since OPTIMIZE / size) | > 20% | Amber |
| Avg file size drop (7d) | > 15% | Amber |

All thresholds are user-overridable on the Config page.

### Schema and catalog rollups

The **+ rollup** buttons add a card that fans out across every managed
Iceberg/Delta table in the chosen catalog or schema. The rollup shows:

- A worst-of badge (red if any table is red, amber if any is amber).
- Counts by badge.
- Aggregate totals (size, files, average file size).
- Mean OPTIMIZE failure rate, most recent OPTIMIZE / VACUUM across the group.
- Top 10 offenders, sorted by severity, with a one-click button to spawn
  a per-table card.
- A toggle to enable/disable PO at the catalog or schema level (inherited
  by tables that don't override).

Defaults to evaluating up to 50 tables; raise via the `max_tables` query
parameter on `/api/po/group_health` if your warehouse can take it.

### Alerts

- Rule types: VACUUM age, OPTIMIZE failure rate, MERGE conflict spike,
  custom threshold.
- Per-rule overrides for Slack webhook and email recipient; falls back to
  the user's defaults, then global defaults.
- Background evaluation tick loop runs continuously in the deployed app.
- Email delivery uses Databricks notification destinations — auto-created
  on first send if one doesn't already point at the recipient.

### Schedules

Cron expressions or trigger-based polling (DESCRIBE HISTORY for new commits).
Schedules persist in UC and fire from a tick loop in the running app. Each
fire is audited.

### Saved dashboards

Per-user named dashboards persist your selected tables across devices.

---

## Configuration

### `scripts/.deploy.env` (gitignored, local only)

Per-deployment settings. Copy `scripts/deploy.env.example` to
`scripts/.deploy.env` and edit:

```bash
WORKSPACE_HOST=https://<your-workspace>.cloud.databricks.com
WAREHOUSE_ID=<warehouse-id>
TARGET=dev                          # optional

# UC catalog/schema where the app stores its state. The app SP needs
# CREATE SCHEMA + CREATE TABLE on the catalog at first boot. Defaults to
# main / po_monitor; most workspaces lock down `main`, so point at a
# catalog the app SP can write to.
PO_MONITOR_CATALOG=
PO_MONITOR_SCHEMA=

# Optional: maintainer email for in-app feedback delivery
PO_MONITOR_MAINTAINER_EMAIL=
```

`deploy.sh` reads this file and injects the `PO_MONITOR_*` values into the
deployed `app.yaml` at deploy time. The file is gitignored, so workspace
configuration never lands in source control.

### Runtime environment variables

| Variable | Default | Purpose |
|---|---|---|
| `DATABRICKS_WAREHOUSE_ID` | (resource-bound) | Default SQL warehouse. Set automatically via the `sql-warehouse` app resource binding; users can override via the sidebar dropdown. |
| `PO_MONITOR_CATALOG` | `main` | UC catalog for app state (config, alerts, audit, schedules, feedback). |
| `PO_MONITOR_SCHEMA` | `po_monitor` | UC schema. Created on first boot. |
| `PO_MONITOR_MAINTAINER_EMAIL` | (unset) | Where in-app feedback is delivered. Unset → UC table only. |
| `PO_MONITOR_DEBUG` | `0` | When truthy, returns full tracebacks on 500 responses. **Production: leave unset.** |

### Persistent state

PO Monitor's own configuration, alert rules, schedules, audit log, and
feedback are persisted in `${PO_MONITOR_CATALOG}.${PO_MONITOR_SCHEMA}.*`.
Tables are bootstrapped idempotently on app startup.

---

## Architecture

```
Browser
   │  X-Forwarded-Access-Token  ┌─────────────┐
   ▼                            │   Browser   │
┌─────────────────────────┐     │             │
│  Databricks Apps proxy  │     │  Sidebar +  │
│  (OAuth, OBO injection) │     │   cards     │
└──────────┬──────────────┘     └─────────────┘
           │  HTTP + headers           ▲
           ▼                           │
┌─────────────────────────────────────────────────┐
│  FastAPI app (PO Monitor)                       │
│   /api/catalog        catalogs/schemas/tables   │
│   /api/po/*           runs, health, group_health│
│   /api/actions/*      OPTIMIZE/VACUUM/toggle    │
│   /api/alerts /sched. CRUD + tick loops         │
│   /api/config         thresholds, warehouse     │
│                                                 │
│   Statement Execution API (per-request OBO)     │
└──────────┬──────────────────────────────────────┘
           ▼
┌──────────────────────────────────────────────┐
│ SQL warehouse  →  Unity Catalog              │
│   user tables                                │
│   system.storage.predictive_optimization_*   │
│   system.query.history                       │
│   system.storage.table_metrics_history       │
│   ${PO_MONITOR_CATALOG}.po_monitor.*  (state)│
└──────────────────────────────────────────────┘
```

- **Backend:** FastAPI (Python 3.11+).
- **Frontend:** React + Vite + TypeScript, Recharts for sparklines.
- **Auth:** OAuth on-behalf-of-user (OBO) via the `X-Forwarded-Access-Token`
  header that Databricks Apps injects. Every data-read runs as the logged-in
  user; UC ACLs are enforced. The app's service principal is used only for
  app-internal state writes (audit, alert rule storage, etc.).
- **SQL path:** Databricks Statement Execution API via `databricks-sdk`.
- **No PATs, no hardcoded tokens** — auth is always either the OBO user
  token or the resource-bound app SP credentials.

---

## Local development

```bash
# Backend
cp .env.example .env       # set DATABRICKS_HOST + DATABRICKS_WAREHOUSE_ID
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
set -a; source .env; set +a
uvicorn app:app --reload --port 8000

# Frontend (separate terminal)
cd frontend
npm install
npm run dev                # vite on :5173, proxies /api → :8000
```

Visit http://localhost:5173. API docs at http://localhost:8000/docs.

For a production-style preview locally:

```bash
cd frontend && npm run build && cd ..
uvicorn app:app --port 8000   # FastAPI now serves frontend/dist/
```

---

## Updating

```bash
git pull
./scripts/deploy.sh
```

`deploy.sh` is idempotent — it rebuilds the frontend, syncs source, refreshes
the warehouse resource binding, and triggers a redeploy. The app does a
zero-downtime swap once the new container is healthy.

---

## Limitations

- **Force PO is a stand-in.** Databricks doesn't expose a public endpoint
  to force the PO scheduler. The "Force PO" button submits OPTIMIZE +
  VACUUM LITE in parallel as the OBO user — the same operations PO would
  run, but on demand.
- **7-day trend charts** depend on the
  `system.storage.table_metrics_history` daily-snapshot table. Tables
  without snapshots return null deltas.
- **PO system-table schema** can change between Databricks runtimes.
  The PO history query handles unknown columns gracefully and surfaces a
  `spike` field on the response if the underlying table isn't found.
- **App logs** require OAuth-U2M auth on the Databricks CLI. PAT auth
  works for everything else but `databricks apps logs` will return
  *OAuth Token not supported for current auth type*.

---

## Releases

See [`CHANGELOG.md`](./CHANGELOG.md). Tagged versions follow semver.

## License

Provided as-is, without warranty (see disclaimer at the top). For internal
or research use; no commercial support is offered.
