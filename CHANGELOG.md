# Changelog

All notable changes to PO Monitor are documented here. The format is loosely
based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this
project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.1] - 2026-05-06

Compaction, group-level controls, and policy-cleanup release. Per-table
PO toggle moves up to the catalog/schema rollup; cards get visibly tighter.

### Added
- **Catalog/schema rollup cards now expose a Predictive Optimization
  enable/disable toggle.** Reflects current state from
  `DESCRIBE {SCHEMA|CATALOG} EXTENDED` and issues
  `ALTER {SCHEMA|CATALOG} … {ENABLE|DISABLE} PREDICTIVE OPTIMIZATION` as
  the OBO user.
- New backend endpoint `/api/actions/toggle_po_group` accepting
  `{kind: 'schema'|'catalog', catalog, schema?, enabled}`.
- `/api/po/group_health` response now carries a `po_state` block with
  `{enabled, raw, inherited}` so the rollup card can render the current
  setting and inheritance status.

### Changed
- **Cards are noticeably more compact** — card padding, KPI tile padding,
  font sizes, action-button height, chart heights all reduced. Grid
  `minmax(560px → 460px)` so more cards fit per row.
- **README rewritten for a customer-facing audience** — drops the
  internal-development "what's real vs stubbed" matrix and "spike notes"
  sections; adds a feature catalog, configuration table, ASCII
  architecture diagram, and a Limitations section.

### Removed
- **Per-table PO toggle removed from `TableCard`.** Predictive
  Optimization enable/disable is intentionally a schema/catalog-level
  control only — keeps policy from fragmenting per-table.
- **Force PO button hidden on `TableCard`.** Pending rewrite against an
  upstream API; backend `/api/actions/force_trigger` and `api.forceTrigger`
  client method remain in place for that rewrite.

## [0.1.0] - 2026-05-04

First public release. PO Monitor is a Databricks App that gives SAs and
customer data-platform teams a live view into Predictive Optimization (PO)
health on Unity Catalog managed Iceberg/Delta tables, plus the buttons to do
something about it.

### Highlights
- **Per-table health badge** with a full ruleset: OPTIMIZE failure rate,
  VACUUM age, unclustered ratio (proxy), 7-day avg file-size drop, and MERGE
  conflict rate. Thresholds are user-overridable on the Config page.
- **Schema and catalog rollup cards** that aggregate health, totals, and top
  offenders across every managed Iceberg/Delta table in the grouping.
- **OBO auth on every data read** via `X-Forwarded-Access-Token` — UC ACLs
  are enforced as the logged-in user, never the app SP.
- **One-shot deploy** (`scripts/deploy.sh`) that builds the frontend, syncs
  source, binds the SQL warehouse, and starts the app.

### Added
**Dashboards & data**
- Catalog → Schema → Table cascade selector; multi-select up to 20 tables;
  selection persists in URL + localStorage; works across catalogs/schemas.
- Per-table KPIs: num files, total size, avg file size, DV count, time
  since OPTIMIZE / VACUUM.
- Trends: file-count sparkline from `files_compacted` across OPTIMIZE runs;
  7-day size deltas vs `system.storage.table_metrics_history`.
- Unified ops table combining PO-driven runs (from
  `system.storage.predictive_optimization_operations_history`) with manual
  operations from `DESCRIBE HISTORY`.
- MERGE conflict surfacing from `system.query.history` —
  `DELTA_CONCURRENT_*` and `DELTA_DUPLICATE_ACTIONS_FOUND` classified.
- **Schema / catalog rollup cards** (`/api/po/group_health`): aggregate
  badge counts, totals, and top offenders across every managed Iceberg/Delta
  table in the grouping. Worker pool of 8 threads, capped at 200 tables.
- Saved dashboards (per user) — name + recall a set of selected tables.

**Maintenance actions**
- OPTIMIZE button — fire-and-forget Statement Execution; returns
  `statement_id` so the UI can poll.
- VACUUM LITE / VACUUM FULL — confirm modal on FULL; same submit pattern.
- ENABLE / DISABLE Predictive Optimization toggle.
- Force PO trigger — submits parallel OPTIMIZE + VACUUM LITE as a stand-in
  for the (private) PO scheduler.
- Schedules — cron and trigger schedules; persisted in UC; async tick loop
  in `server/scheduler.py` fires them.
- Audit log persisted to `${PO_MONITOR_CATALOG}.po_monitor.audit`.

**Alerts**
- Rule CRUD persisted in UC.
- Background evaluation tick loop in `server/alerts_engine.py`.
- Slack webhook delivery (per-rule override → user → global).
- Email delivery via Databricks notification destinations (auto-created on
  first send).
- Rule types: VACUUM age, OPTIMIZE failure rate, MERGE conflict spike, and
  custom thresholds.

**Deploy & ops**
- `scripts/deploy.sh` — build → sync → inject env from `.deploy.env`
  → bind sql-warehouse resource → deploy.
- `scripts/.deploy.env` (gitignored) carries workspace host, warehouse ID,
  and `PO_MONITOR_*` overrides; values are injected into the deployed
  `app.yaml` at deploy time so they never enter source.
- `databricks.yml` Asset Bundle and `app.yaml` runtime config.
- UC bootstrap (`server/db.py`) creates the schema/tables idempotently
  on app startup; surfaces failures via `/api/health`.
- Warehouse preflight check on app startup; surfaces drift via
  `/api/health.warehouse_preflight`.
- Configurable via env: `PO_MONITOR_CATALOG`, `PO_MONITOR_SCHEMA`,
  `PO_MONITOR_MAINTAINER_EMAIL`, `PO_MONITOR_DEBUG`.

**UI**
- Warehouse picker modal blocks the dashboard until a warehouse is selected
  on first run; sidebar dropdown defaults to the deployed warehouse on
  subsequent loads.
- Per-user auto-refresh interval (off / 1m / 5m / 30m / 60m).
- Dark theme with Databricks-ish accents.
- In-app feedback form that delivers via the maintainer's notification
  destination.

### Security
- SQL identifier validation (`validate_ident`, `escape_ident`) at every
  route boundary; rejects backtick break-outs in catalog/schema/table names.
- `ThreadPoolExecutor` workers in `group_health` and `force_trigger` snapshot
  `WAREHOUSE_OVERRIDE` and re-set it on each thread's context (Python's
  `Context.run` can't be entered concurrently from multiple threads).
- Global exception handler gated on `PO_MONITOR_DEBUG`; production responses
  return generic `internal error` with the full traceback in the server log
  only.
- `/api/catalog/diag` returns an 8-char SHA256 fingerprint of the OBO token
  rather than echoing a 20-char prefix.
- `useSelection` shape-validates URL params and localStorage; entries failing
  the identifier regex are silently dropped.
- TableCard / GroupCard refresh use a fetch-generation guard so stale fetches
  from prop-change/unmount/race no longer commit.
- `_resolve_managed_tables` capped at 100 schemas and `max_tables * 4`
  DESCRIBE budgets to bound work on adversarial / huge catalogs.
- All workspace-specific identifiers (warehouse IDs, catalog name, SP UUID,
  maintainer email, workspace host) scrubbed from committed source.

### Known gaps
- The Force PO trigger is a stand-in (OPTIMIZE + VACUUM LITE) — Databricks
  has no public SQL or REST endpoint to force the PO scheduler.
- `system.storage.table_metrics_history` is required for 7-day file-count /
  size trends; tables without snapshots return null deltas.
- Avg-file-size trend depends on the same daily snapshot table.
- `apps logs` requires OAuth-U2M auth, not PAT — we surface "App started
  successfully" and `/api/health.warehouse_preflight` as healthy/unhealthy
  signals; the actual stdout is only visible to OAuth-authenticated callers.

### Disclaimer
Provided as-is, without warranty of any kind. Not an official Databricks
product; no Databricks support. Use at your own risk.
