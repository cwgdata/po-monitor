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
      catalog.py             # /api/catalog/* — catalogs/schemas/tables
      po.py                  # /api/po/* — PO runs, desc detail, health, group rollups, merges
      actions.py             # /api/actions/* — OPTIMIZE/VACUUM/toggle/force-trigger/audit
      alerts.py              # /api/alerts/* — rule CRUD + on-demand /test
      schedules.py           # /api/schedules/* — cron + trigger schedules
      dashboards.py          # /api/dashboards/* — saved dashboard configs (per-user)
      feedback.py            # /api/feedback — submission → notification destinations
      card_cache.py          # /api/card-cache — last-known card payload, per user
      config.py              # /api/config — runtime config
    db.py                    # UC bootstrap + persistence helpers (config, audit, alerts, schedules…)
    alerts_engine.py         # Alert evaluation tick loop (asyncio task)
    alerts_dispatch.py       # Slack webhook + Databricks email destination delivery
    scheduler.py             # Schedule firing tick loop (asyncio task)
    sql_client.py            # Statement Execution wrapper
  sql/
    po_runs.sql              # PO system-table query
    desc_detail.sql          # DESC DETAIL extract
    list_managed_iceberg.sql # UC info_schema query
  frontend/
    package.json  vite.config.ts  tsconfig.json  index.html
    src/
      App.tsx  main.tsx  styles.css
      hooks/useSelection.ts  # tables[] + groups[] + persistence
      lib/api.ts
      components/
        Selector.tsx         # Catalog → Schema → Table cascade + rollup buttons
        TableCard.tsx        # Per-table dashboard tile
        GroupCard.tsx        # Schema/catalog rollup tile
        AlertsPanel.tsx  ConfigPanel.tsx  SchedulesPanel.tsx
        FeedbackModal.tsx  Toggle.tsx
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
| Schema / catalog rollup cards | REAL | `/api/po/group_health` aggregates badge counts + totals + top offenders across every managed Iceberg/Delta table in the grouping. Add via "+ rollup" buttons in the sidebar |
| `DESC DETAIL` KPIs (num files, size, avg size) | REAL | `/api/po/detail` |
| PO run history + status table | REAL (SPIKE) | Queries `system.storage.predictive_optimization_operations_history`; graceful empty + spike note if system table name differs |
| Health badge | REAL | Full ruleset: OPTIMIZE failure rate, VACUUM age, unclustered ratio, 7d avg-size drop. Thresholds editable on Config page |
| File-count trend sparkline | REAL | Derived from `files_compacted` across OPTIMIZE runs |
| Avg file size trend (7d) | REAL | Compared against `system.storage.table_metrics_history` snapshot |
| DV count | REAL | From `DESC DETAIL` (when DBR exposes it); else null and surfaced as "n/a" |
| Unclustered ratio (proxy) | REAL | bytes_added since last OPTIMIZE / current size_bytes — surfaced via `unclustered_proxy.ratio` |
| Time since OPTIMIZE / VACUUM | REAL | Computed server-side from run history; `vacuum_age_days` in /api/po/health |
| MERGE conflict rate | REAL | `/api/po/merges` queries `system.query.history` for MERGE statements; classifies failures by `DELTA_CONCURRENT_*` error pattern. Surfaced as a tile + recent-queries table on each card, factored into the health badge, and available as the `MERGE_CONFLICT_SPIKE` alert rule type |
| OPTIMIZE button | REAL | Statement Execution, fire-and-forget. Returns statement_id so UI can poll |
| VACUUM LITE / FULL buttons | REAL | Same pattern; FULL confirm modal wired |
| Enable / Disable PO toggle | REAL | `ALTER TABLE … {ENABLE\|DISABLE} PREDICTIVE OPTIMIZATION` |
| Force PO trigger | REAL | Stand-in: submits OPTIMIZE + VACUUM LITE as the OBO user (PO scheduler has no public force endpoint) |
| Schedule OPTIMIZE / VACUUM | REAL | Cron + trigger schedules persisted in UC; background tick loop in `server/scheduler.py` |
| Audit log | REAL | Persisted to `${PO_MONITOR_CATALOG}.po_monitor.audit`, 100-row default page |
| Alerts: rule CRUD | REAL | Persisted to `${PO_MONITOR_CATALOG}.po_monitor.alert_rules` |
| Alerts: Slack webhook delivery | REAL | `server/alerts_dispatch.py` — webhook resolved per-rule → user → global |
| Alerts: evaluation loop | REAL | `server/alerts_engine.py` runs as a background asyncio task in lifespan |
| Email delivery | REAL | Via Databricks notification destinations, auto-created on first send |
| Config page | REAL | View + patch global thresholds, Slack webhook, alert email — persisted in UC |
| Persistence of config/rules/audit | REAL | All in `${PO_MONITOR_CATALOG}.po_monitor.*` (see `server/db.py`) |
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
