# PO Monitor

Databricks App for monitoring **Predictive Optimization (PO)** on Unity Catalog managed Iceberg tables. Built for SAs / customer data platform teams operating UC managed Iceberg at scale (PB-class, external writers like EMR Spark/Flink).

> **Disclaimer.** This project is provided **as-is**, without warranty of any kind, express or implied (including merchantability, fitness for a particular purpose, and non-infringement). It is **not an official Databricks product** and is not supported by Databricks. Use at your own risk; the authors and contributors accept no liability for any damages or data loss arising from its use. Review the code and verify behavior against your own environment before relying on it.

Quick install:

```bash
git clone <repo> po-monitor && cd po-monitor
./scripts/deploy.sh https://<your-workspace>.cloud.databricks.com <warehouse_id>
```

That single command builds the frontend, syncs source to your workspace, creates/updates the Databricks App, and binds the SQL warehouse with the right SP grants. Read `## Deploy` below for prereqs.

## Stack

- **Backend**: FastAPI (Python) — chosen over Node for first-class Databricks SDK + Statement Execution support.
- **Frontend**: React + Vite + TypeScript. Recharts for trend sparklines.
- **Auth**: OAuth on-behalf-of-user (OBO) via `X-Forwarded-Access-Token` header that Databricks Apps injects. Every data-read and every maintenance action runs as the logged-in user so UC ACLs are enforced. No PAT, no hardcoded tokens.
- **SQL path**: Databricks Statement Execution API (via `databricks-sdk`). Warehouse bound as an app resource (`sql-warehouse` → `DATABRICKS_WAREHOUSE_ID`).

## Directory tree

```
po-monitor/
  app.py                     # FastAPI entry, mounts routers + React dist
  app.yaml                   # Databricks App config
  requirements.txt
  .gitignore
  server/
    config.py                # Dual-mode auth helpers, thresholds
    sql_client.py            # Statement Execution wrapper
    routes/
      catalog.py             # /api/catalog/* — catalogs/schemas/tables (REAL)
      po.py                  # /api/po/* — PO runs, desc detail, health (REAL)
      actions.py             # /api/actions/* — OPTIMIZE/VACUUM/toggle/schedule (mixed)
      alerts.py              # /api/alerts/* — rule CRUD, Slack test (rules real, eval stubbed)
      config.py              # /api/config — runtime config
  sql/
    po_runs.sql              # PO system-table query (SPIKE)
    desc_detail.sql          # DESC DETAIL stub
    list_managed_iceberg.sql # UC info_schema query
  frontend/
    package.json  vite.config.ts  tsconfig.json  index.html
    src/
      App.tsx  main.tsx  styles.css
      hooks/useSelection.ts
      lib/api.ts
      components/
        Selector.tsx         # Catalog → Schema → Table cascade
        TableCard.tsx        # Per-table dashboard tile
        AlertsPanel.tsx
        ConfigPanel.tsx
```

## Run locally

### Prereqs

- Python 3.11+, Node 18+
- Databricks CLI v0.260+, authenticated against the target workspace (`databricks auth login`)
- A SQL warehouse ID

### Backend

```bash
cp .env.example .env       # edit with your host + warehouse ID
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# load env then run
set -a; source .env; set +a
uvicorn app:app --reload --port 8000
```

API docs at http://localhost:8000/docs.

### Frontend

```bash
cd frontend
npm install
npm run dev        # vite dev server on :5173, proxies /api → :8000
```

Visit http://localhost:5173.

### Build for deployment (manual)

```bash
cd frontend && npm run build     # outputs frontend/dist/
cd ..
# FastAPI auto-serves frontend/dist/ if present. Verify by hitting :8000 directly.
```

## Deploy

### One-shot

```bash
./scripts/deploy.sh https://<workspace>.cloud.databricks.com <warehouse_id> [target]
```

What it does:
1. `npm run build` for the frontend
2. `databricks bundle validate` (Asset Bundle in `databricks.yml`)
3. `databricks bundle deploy` — syncs source + creates/updates the app + binds the SQL warehouse resource (auto-grants SP `CAN_USE`)
4. `databricks bundle run po_monitor` — starts the app

Targets in the bundle: `dev` (default), `prod`. Add more by extending `databricks.yml` → `targets:`.

### Verify after deploy

Open the app, check **`/api/health`**:

```json
{
  "ok": true,
  "bootstrap": { ... },
  "warehouse_preflight": { "ok": true, "message": "SP CAN_USE warehouse <id>" }
}
```

If `warehouse_preflight.ok` is `false`, the message includes the exact `databricks api put` to add `CAN_USE` for the app's service principal. The bundle resource binding should set this automatically — drift only happens if the warehouse is recreated or its ACL is reset out-of-band.

### Required workspace permissions

To deploy, the user running `deploy.sh` needs:
- `CAN_MANAGE` on the target workspace (to create the app)
- `CAN_USE` on the chosen warehouse (so resource binding can grant it forward to the app SP)
- Permission to write under `/Workspace/Users/<you>/.bundle/po-monitor/`

The runtime app SP automatically inherits `CAN_USE` on the bound warehouse. End users running OBO queries need their own warehouse + UC privileges.

## The one real end-to-end path

1. Sidebar **Selector** calls `GET /api/catalog/catalogs` → `GET /api/catalog/schemas?catalog=` → `GET /api/catalog/tables?catalog=&schema=&iceberg_only=true`. Each request flows through `get_user_client()` which honors OBO from the proxy header.
2. User checks one or more tables (persisted to URL + localStorage).
3. For each selected table, `TableCard` calls:
   - `GET /api/po/health?catalog=&schema=&table=` — rolls up `DESC DETAIL` + PO run history into a green/amber/red badge
   - `GET /api/po/runs?...&lookback_days=30` — recent PO operations table + sparkline
4. PO runs come from `system.storage.predictive_optimization_operations_history` (see spike note below).

Every data read is authorized as the logged-in user.

## What's real vs stubbed

| Feature | Status | Notes |
|---|---|---|
| Catalog/schema/table cascade | REAL | `/api/catalog/*`, filters to managed Iceberg via `information_schema.tables` |
| Multi-select, 20-table cap | REAL | `useSelection` hook, URL + localStorage persistence |
| `DESC DETAIL` KPIs (num files, size, avg size) | REAL | `/api/po/detail` |
| PO run history + status table | REAL (SPIKE) | Queries `system.storage.predictive_optimization_operations_history`; graceful empty + spike note if system table name differs |
| Health badge | PARTIAL | Only OPTIMIZE-failure-rate rule wired. TODO: VACUUM age, unclustered %, 3d avg-size-drop |
| File-count trend sparkline | REAL | Derived from `files_compacted` across OPTIMIZE runs |
| Avg file size trend over time | STUB | Need a daily snapshot table — `DESC DETAIL` is point-in-time only |
| DV count trend | STUB | Column `num_deletion_vectors_removed` in PO metrics is per-run, not absolute — need source |
| Unclustered bytes | STUB | `DESC DETAIL` may expose in newer DBR; else probe liquid-clustering metrics |
| Time since OPTIMIZE / VACUUM | REAL | Computed client-side from run history |
| MERGE conflict rate | STUB | TODO: query `system.access.audit` for `DELTA_CONCURRENT_DELETE_DELETE` / `DELTA_DUPLICATE_ACTIONS_FOUND` errors in last 24h |
| OPTIMIZE button | REAL | Statement Execution synchronous. TODO: switch to Jobs API for long-running compactions |
| VACUUM LITE / FULL buttons | REAL (SQL) | Fires `VACUUM … LITE/FULL`; FULL confirm modal wired. TODO: Jobs API |
| Enable / Disable PO toggle | REAL | `ALTER TABLE … SET TBLPROPERTIES` |
| Force PO trigger | STUB (501) | No public SQL to force PO; comment-tracked |
| Schedule OPTIMIZE / VACUUM | STUB (501) | TODO: Jobs API `jobs.create` with SqlTask + quartz_cron_expression |
| Audit log | PARTIAL | In-memory list exposed at `/api/actions/audit`. TODO: persist to UC table `po_monitor_audit` |
| Alerts: rule CRUD | REAL | In-memory; `/api/alerts` |
| Alerts: Slack webhook delivery | REAL (manual) | `POST /api/alerts/test` fires a test payload |
| Alerts: evaluation loop | STUB | Need APScheduler or a separate Databricks Job to poll rules |
| Email delivery | STUB | Pick SMTP or Databricks Job notification_settings |
| Config page | REAL | View + patch defaults, Slack webhook, thresholds (in-memory) |
| Persistence of config/rules/audit | STUB | Currently in-memory. TODO: Lakebase or UC table backing |
| Dark UI | REAL | Custom CSS, Databricks-ish orange accent |

## Spike notes / probe queries

The PO system-table schema is the biggest unknown. In the target workspace, run:

```sql
SHOW TABLES IN system.storage LIKE '*predictive*';
SHOW TABLES IN system.lakeflow LIKE '*optim*';
DESCRIBE system.storage.predictive_optimization_operations_history;
```

Adjust `server/routes/po.py` and `sql/po_runs.sql` column names once confirmed. The code already returns a clear `spike` message in the API response if the table isn't found, so the frontend doesn't crash.

## Security

- No tokens hardcoded anywhere.
- `DATABRICKS_WAREHOUSE_ID` comes from resource binding (`valueFrom: sql-warehouse` in `app.yaml`).
- User OBO is enforced per-request by `get_user_client()`. When deployed, the Databricks Apps runtime injects `X-Forwarded-Access-Token`. Locally, it falls back to your CLI profile.
- Action endpoints audit `who/what/when/result` (user email pulled from `X-Forwarded-Email`).

## Files added for distribution

| File | Purpose |
|---|---|
| `databricks.yml` | Asset Bundle — declares the app + warehouse resource binding for `dev` / `prod` targets |
| `scripts/deploy.sh` | One-shot deploy: build → validate → deploy → run |
| `.env.example` | Local-dev config template (copy to `.env`, never committed) |
| `app.yaml` | Databricks Apps runtime config — uvicorn command, env mapping, OBO scopes |
