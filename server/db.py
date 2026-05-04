"""UC-backed persistence for PO Monitor.

All writes use the *app service principal* (not OBO). Rationale:
  - Audit integrity: every user action must land in the audit table
    even if that user lacks MODIFY on the audit table itself.
  - Bootstrap: schema/table creation is SP-owned so the app owns its
    state surface end-to-end.
  - Config is app-global (not user-scoped); OBO would either fail for
    users who lack catalog writes, or surface per-user state we don't want.

Read paths (list_alert_rules, list_audit, etc.) also use SP for simplicity
since the schema is SP-owned and only consumed by this app. If you ever
expose these reads to end-users who should only see their own rows, flip
those to OBO per-call.

Tables (in `${CATALOG}.${SCHEMA}` — defaults `main.po_monitor`, override via env):
  config             — key/value app config
  alert_rules        — alert rule definitions
  audit              — action audit log
  schedules          — scheduled maintenance jobs
  feedback           — in-app feedback submissions
  dashboard_configs  — per-user saved dashboards
"""
from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from databricks.sdk import WorkspaceClient

from .config import DEFAULT_THRESHOLDS, get_app_client
from .sql_client import execute_sql, rows_as_dicts

# Override via env (set in app.yaml or local .env). The app SP needs
# CREATE SCHEMA / CREATE TABLE on this catalog at first boot for bootstrap.
CATALOG = os.environ.get("PO_MONITOR_CATALOG", "main")
SCHEMA = os.environ.get("PO_MONITOR_SCHEMA", "po_monitor")
FQ_SCHEMA = f"`{CATALOG}`.`{SCHEMA}`"


# ----------------------------------------------------------------------------
# Bootstrap — idempotent; called on app startup
# ----------------------------------------------------------------------------

_BOOTSTRAP_STATE: dict[str, Any] = {
    "ran": False,
    "ok": False,
    "error": None,
    "counts": {},
}


def _sp_client() -> WorkspaceClient:
    """App service principal client. Writes + bootstrap go through this."""
    return get_app_client()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def bootstrap() -> dict[str, Any]:
    """Create schema + tables if missing. Seed default thresholds.

    Returns a dict describing the outcome: {ok: bool, error: str|None, counts: {...}}
    """
    global _BOOTSTRAP_STATE
    _BOOTSTRAP_STATE["ran"] = True
    try:
        client = _sp_client()

        # 1. Schema
        execute_sql(client, f"CREATE SCHEMA IF NOT EXISTS {FQ_SCHEMA}")

        # 2. Tables — managed Iceberg, clustered on most-queried columns.
        # NOTE on TBLPROPERTIES: managed Iceberg + CLUSTER BY requires
        # deletion vectors + row tracking to be disabled (server error
        # MANAGED_ICEBERG_ATTEMPTED_TO_ENABLE_CLUSTERING_WITHOUT_DISABLING_DVS_OR_ROW_TRACKING).
        # That's fine here — concurrency on these tiny app-state tables is
        # low and we don't need DV-based merges.
        ICE_PROPS = (
            "TBLPROPERTIES ('delta.enableDeletionVectors'='false',"
            "'delta.enableRowTracking'='false')"
        )

        execute_sql(client, f"""
            CREATE TABLE IF NOT EXISTS {FQ_SCHEMA}.config (
                config_key   STRING NOT NULL,
                config_value STRING,
                updated_at   TIMESTAMP,
                updated_by   STRING
            )
            USING ICEBERG
            CLUSTER BY (config_key)
            {ICE_PROPS}
        """)

        execute_sql(client, f"""
            CREATE TABLE IF NOT EXISTS {FQ_SCHEMA}.alert_rules (
                rule_id        STRING NOT NULL,
                catalog        STRING,
                schema_name    STRING,
                table_name     STRING,
                rule_type      STRING,
                threshold      DOUBLE,
                lookback_minutes INT,
                enabled        BOOLEAN,
                slack_webhook  STRING,
                email          STRING,
                created_by     STRING,
                created_at     TIMESTAMP,
                updated_at     TIMESTAMP,
                last_evaluated_at TIMESTAMP,
                last_fired_at  TIMESTAMP,
                last_status    STRING,
                last_error     STRING
            )
            USING ICEBERG
            CLUSTER BY (catalog, schema_name, table_name)
            {ICE_PROPS}
        """)

        # alert_events — every fire writes a row here. Used by the UI's
        # "recent fires" panel and by the per-table-per-day dedupe key for
        # wildcard rules.
        execute_sql(client, f"""
            CREATE TABLE IF NOT EXISTS {FQ_SCHEMA}.alert_events (
                event_id       STRING NOT NULL,
                rule_id        STRING NOT NULL,
                fired_at       TIMESTAMP,
                catalog        STRING,
                schema_name    STRING,
                table_name     STRING,
                rule_type      STRING,
                threshold      DOUBLE,
                observed_value DOUBLE,
                message        STRING,
                delivery       STRING,
                delivery_error STRING
            )
            USING ICEBERG
            CLUSTER BY (rule_id, fired_at)
            {ICE_PROPS}
        """)

        execute_sql(client, f"""
            CREATE TABLE IF NOT EXISTS {FQ_SCHEMA}.audit (
                event_id   STRING NOT NULL,
                ts         TIMESTAMP,
                user_email STRING,
                action     STRING,
                target     STRING,
                result     STRING,
                meta       STRING
            )
            USING ICEBERG
            CLUSTER BY (ts)
            {ICE_PROPS}
        """)

        execute_sql(client, f"""
            CREATE TABLE IF NOT EXISTS {FQ_SCHEMA}.schedules (
                schedule_id STRING NOT NULL,
                catalog     STRING,
                schema_name STRING,
                table_name  STRING,
                operation   STRING,
                cron        STRING,
                timezone    STRING,
                job_id      STRING,
                enabled     BOOLEAN,
                created_by  STRING,
                created_at  TIMESTAMP,
                updated_at  TIMESTAMP
            )
            USING ICEBERG
            CLUSTER BY (catalog, schema_name, table_name)
            {ICE_PROPS}
        """)

        execute_sql(client, f"""
            CREATE TABLE IF NOT EXISTS {FQ_SCHEMA}.feedback (
                feedback_id  STRING NOT NULL,
                ts           TIMESTAMP,
                user_email   STRING,
                subject      STRING,
                message      STRING,
                app_url      STRING,
                user_agent   STRING,
                delivery     STRING,
                delivery_error STRING
            )
            USING ICEBERG
            CLUSTER BY (ts)
            {ICE_PROPS}
        """)

        # Saved dashboards (per-user table selections). Scoped by user_email —
        # every read/write repo function checks the caller email.
        execute_sql(client, f"""
            CREATE TABLE IF NOT EXISTS {FQ_SCHEMA}.dashboard_configs (
                config_id    STRING NOT NULL,
                user_email   STRING NOT NULL,
                name         STRING NOT NULL,
                tables_json  STRING NOT NULL,
                created_at   TIMESTAMP,
                updated_at   TIMESTAMP
            )
            USING ICEBERG
            CLUSTER BY (user_email)
            {ICE_PROPS}
        """)

        # Per-user per-table card cache. Lets a fresh browser / device paint
        # last-known data before the live fetch completes. Keyed by
        # (user_email, catalog, schema_name, table_name).
        execute_sql(client, f"""
            CREATE TABLE IF NOT EXISTS {FQ_SCHEMA}.card_cache (
                user_email   STRING NOT NULL,
                catalog      STRING NOT NULL,
                schema_name  STRING NOT NULL,
                table_name   STRING NOT NULL,
                payload_json STRING NOT NULL,
                updated_at   TIMESTAMP
            )
            USING ICEBERG
            CLUSTER BY (user_email, catalog, schema_name, table_name)
            {ICE_PROPS}
        """)

        # Schedules schema has evolved (v2). We can't use ADD COLUMNS IF NOT
        # EXISTS — ALTER TABLE doesn't support it — so describe first and add
        # each missing column one-by-one. Safe and idempotent across restarts.
        _migrate_schedules_columns(client)
        _migrate_alert_rules_columns(client)

        # 3. Seed default thresholds on first run (MERGE — don't overwrite)
        _seed_defaults(client)

        # 4. Row counts for verification
        counts = {}
        for tbl in ("config", "alert_rules", "alert_events", "audit", "schedules", "feedback", "dashboard_configs", "card_cache"):
            try:
                r = execute_sql(client, f"SELECT COUNT(*) AS n FROM {FQ_SCHEMA}.{tbl}")
                counts[tbl] = r["rows"][0][0] if r["rows"] else 0
            except Exception:
                counts[tbl] = None

        _BOOTSTRAP_STATE = {"ran": True, "ok": True, "error": None, "counts": counts}
        return _BOOTSTRAP_STATE
    except Exception as e:
        _BOOTSTRAP_STATE = {"ran": True, "ok": False, "error": str(e), "counts": {}}
        return _BOOTSTRAP_STATE


def bootstrap_state() -> dict[str, Any]:
    return dict(_BOOTSTRAP_STATE)


_SCHEDULE_V2_COLUMNS: list[tuple[str, str]] = [
    ("schedule_type", "STRING"),
    ("poll_interval_seconds", "INT"),
    ("min_interval_seconds", "INT"),
    ("warehouse_id", "STRING"),
    ("user_email", "STRING"),
    ("last_run_at", "TIMESTAMP"),
    ("last_checked_at", "TIMESTAMP"),
    ("last_run_status", "STRING"),
    ("last_run_error", "STRING"),
    ("last_run_statement_id", "STRING"),
]


def _migrate_schedules_columns(client: WorkspaceClient) -> None:
    """Idempotently add v2 columns to the schedules table.

    ALTER TABLE ADD COLUMNS doesn't support IF NOT EXISTS; describe first,
    then add only the missing columns. Safe to call on every boot.
    """
    try:
        existing = execute_sql(client, f"DESCRIBE TABLE {FQ_SCHEMA}.schedules")
    except Exception as e:
        print(f"[bootstrap] describe schedules failed: {e}")
        return
    have: set[str] = set()
    for row in existing.get("rows", []):
        if not row:
            continue
        col = (row[0] or "").strip()
        # DESCRIBE returns a separator row starting with '#' before partition/cluster info.
        if not col or col.startswith("#"):
            continue
        have.add(col)
    missing = [(n, t) for (n, t) in _SCHEDULE_V2_COLUMNS if n not in have]
    if not missing:
        return
    cols_sql = ", ".join(f"{n} {t}" for (n, t) in missing)
    try:
        execute_sql(client, f"ALTER TABLE {FQ_SCHEMA}.schedules ADD COLUMNS ({cols_sql})")
        print(f"[bootstrap] schedules table migrated: added {[n for n,_ in missing]}")
    except Exception as e:
        print(f"[bootstrap] schedules migration failed (non-fatal): {e}")


_ALERT_RULES_V2_COLUMNS: list[tuple[str, str]] = [
    ("last_evaluated_at", "TIMESTAMP"),
    ("last_fired_at", "TIMESTAMP"),
    ("last_status", "STRING"),
    ("last_error", "STRING"),
]


def _migrate_alert_rules_columns(client: WorkspaceClient) -> None:
    """Idempotently add tracking columns to alert_rules. Same pattern as schedules."""
    try:
        existing = execute_sql(client, f"DESCRIBE TABLE {FQ_SCHEMA}.alert_rules")
    except Exception as e:
        print(f"[bootstrap] describe alert_rules failed: {e}")
        return
    have: set[str] = set()
    for row in existing.get("rows", []):
        if not row:
            continue
        col = (row[0] or "").strip()
        if not col or col.startswith("#"):
            continue
        have.add(col)
    missing = [(n, t) for (n, t) in _ALERT_RULES_V2_COLUMNS if n not in have]
    if not missing:
        return
    cols_sql = ", ".join(f"{n} {t}" for (n, t) in missing)
    try:
        execute_sql(client, f"ALTER TABLE {FQ_SCHEMA}.alert_rules ADD COLUMNS ({cols_sql})")
        print(f"[bootstrap] alert_rules table migrated: added {[n for n,_ in missing]}")
    except Exception as e:
        print(f"[bootstrap] alert_rules migration failed (non-fatal): {e}")


def _seed_defaults(client: WorkspaceClient) -> None:
    """Insert DEFAULT_THRESHOLDS into config table if not already present.

    Uses MERGE with WHEN NOT MATCHED INSERT to be idempotent across restarts.
    """
    for key, value in DEFAULT_THRESHOLDS.items():
        sql = f"""
            MERGE INTO {FQ_SCHEMA}.config t
            USING (SELECT :k AS config_key, :v AS config_value) s
            ON t.config_key = s.config_key
            WHEN NOT MATCHED THEN INSERT (config_key, config_value, updated_at, updated_by)
              VALUES (s.config_key, s.config_value, current_timestamp(), 'bootstrap')
        """
        execute_sql(
            client,
            sql,
            parameters=[
                {"name": "k", "value": key, "type": "STRING"},
                {"name": "v", "value": str(value), "type": "STRING"},
            ],
        )


# ----------------------------------------------------------------------------
# config key/value repo
# ----------------------------------------------------------------------------

def config_list(user_email: Optional[str] = None) -> dict[str, str]:
    """Return config as {key: value}, user-scoped with fallback to global.

    Global rows have user_email IS NULL (seeded by bootstrap). Per-user rows
    override globals. When user_email is None, returns just global defaults.
    """
    client = _sp_client()
    if user_email:
        # Prefer per-user value; fall back to global (user_email IS NULL)
        result = execute_sql(
            client,
            f"""
            WITH user_rows AS (
              SELECT config_key, config_value
              FROM {FQ_SCHEMA}.config
              WHERE user_email = :u
            ),
            global_rows AS (
              SELECT config_key, config_value
              FROM {FQ_SCHEMA}.config
              WHERE user_email IS NULL
            )
            SELECT COALESCE(u.config_key, g.config_key) AS config_key,
                   COALESCE(u.config_value, g.config_value) AS config_value
            FROM global_rows g
            FULL OUTER JOIN user_rows u ON g.config_key = u.config_key
            """,
            parameters=[{"name": "u", "value": user_email, "type": "STRING"}],
        )
    else:
        result = execute_sql(
            client,
            f"SELECT config_key, config_value FROM {FQ_SCHEMA}.config WHERE user_email IS NULL",
        )
    out: dict[str, str] = {}
    for row in result["rows"]:
        if row and row[0] is not None:
            out[row[0]] = row[1]
    return out


def config_get(key: str, user_email: Optional[str] = None) -> Optional[str]:
    """Get a single config value. Returns user's value if set, else global default."""
    client = _sp_client()
    if user_email:
        result = execute_sql(
            client,
            f"""
            SELECT config_value FROM {FQ_SCHEMA}.config
            WHERE config_key = :k AND user_email = :u
            """,
            parameters=[
                {"name": "k", "value": key, "type": "STRING"},
                {"name": "u", "value": user_email, "type": "STRING"},
            ],
        )
        if result["rows"]:
            return result["rows"][0][0]
    result = execute_sql(
        client,
        f"SELECT config_value FROM {FQ_SCHEMA}.config WHERE config_key = :k AND user_email IS NULL",
        parameters=[{"name": "k", "value": key, "type": "STRING"}],
    )
    rows = result["rows"]
    return rows[0][0] if rows else None


def config_set(key: str, value: Optional[str], user: str, user_email: Optional[str] = None) -> None:
    """Upsert a config key for a specific user (or global if user_email is None).

    Writes a per-user row when user_email is provided; otherwise writes the
    global-default row (user_email IS NULL). `user` is just the actor identity
    recorded in updated_by for audit.
    """
    client = _sp_client()
    if value is None:
        # Delete just this user's row (or global if no user_email)
        if user_email:
            execute_sql(
                client,
                f"DELETE FROM {FQ_SCHEMA}.config WHERE config_key = :k AND user_email = :u",
                parameters=[
                    {"name": "k", "value": key, "type": "STRING"},
                    {"name": "u", "value": user_email, "type": "STRING"},
                ],
            )
        else:
            execute_sql(
                client,
                f"DELETE FROM {FQ_SCHEMA}.config WHERE config_key = :k AND user_email IS NULL",
                parameters=[{"name": "k", "value": key, "type": "STRING"}],
            )
        return

    # Match on (config_key, user_email) — handle NULL via IS NOT DISTINCT FROM
    if user_email:
        sql = f"""
            MERGE INTO {FQ_SCHEMA}.config t
            USING (SELECT :k AS config_key, :v AS config_value, :u AS updated_by, :ue AS user_email) s
            ON t.config_key = s.config_key AND t.user_email = s.user_email
            WHEN MATCHED THEN UPDATE SET
                config_value = s.config_value,
                updated_at   = current_timestamp(),
                updated_by   = s.updated_by
            WHEN NOT MATCHED THEN INSERT (config_key, config_value, updated_at, updated_by, user_email)
              VALUES (s.config_key, s.config_value, current_timestamp(), s.updated_by, s.user_email)
        """
        params = [
            {"name": "k", "value": key, "type": "STRING"},
            {"name": "v", "value": str(value), "type": "STRING"},
            {"name": "u", "value": user, "type": "STRING"},
            {"name": "ue", "value": user_email, "type": "STRING"},
        ]
    else:
        sql = f"""
            MERGE INTO {FQ_SCHEMA}.config t
            USING (SELECT :k AS config_key, :v AS config_value, :u AS updated_by) s
            ON t.config_key = s.config_key AND t.user_email IS NULL
            WHEN MATCHED THEN UPDATE SET
                config_value = s.config_value,
                updated_at   = current_timestamp(),
                updated_by   = s.updated_by
            WHEN NOT MATCHED THEN INSERT (config_key, config_value, updated_at, updated_by, user_email)
              VALUES (s.config_key, s.config_value, current_timestamp(), s.updated_by, NULL)
        """
        params = [
            {"name": "k", "value": key, "type": "STRING"},
            {"name": "v", "value": str(value), "type": "STRING"},
            {"name": "u", "value": user, "type": "STRING"},
        ]
    execute_sql(client, sql, parameters=params)


def config_set_many(items: dict[str, Any], user: str, user_email: Optional[str] = None) -> None:
    """Upsert many config keys for a specific user."""
    for k, v in items.items():
        config_set(k, None if v is None else str(v), user, user_email=user_email)


# ----------------------------------------------------------------------------
# feedback repo
# ----------------------------------------------------------------------------

def feedback_append(
    user_email: Optional[str],
    subject: str,
    message: str,
    app_url: Optional[str],
    user_agent: Optional[str],
    delivery: str = "recorded",
    delivery_error: Optional[str] = None,
) -> str:
    """Insert a feedback row. Returns feedback_id."""
    client = _sp_client()
    fid = str(uuid.uuid4())
    execute_sql(
        client,
        f"""
        INSERT INTO {FQ_SCHEMA}.feedback
          (feedback_id, ts, user_email, subject, message, app_url, user_agent, delivery, delivery_error)
        VALUES (:fid, current_timestamp(), :ue, :sub, :msg, :url, :ua, :dl, :de)
        """,
        parameters=[
            {"name": "fid", "value": fid, "type": "STRING"},
            {"name": "ue", "value": user_email or "", "type": "STRING"},
            {"name": "sub", "value": subject, "type": "STRING"},
            {"name": "msg", "value": message, "type": "STRING"},
            {"name": "url", "value": app_url or "", "type": "STRING"},
            {"name": "ua", "value": user_agent or "", "type": "STRING"},
            {"name": "dl", "value": delivery, "type": "STRING"},
            {"name": "de", "value": delivery_error or "", "type": "STRING"},
        ],
    )
    return fid


# ----------------------------------------------------------------------------
# alert_rules repo
# ----------------------------------------------------------------------------

def alert_rule_upsert(rule: dict[str, Any], user: str) -> str:
    """Insert or update an alert rule. Returns rule_id."""
    client = _sp_client()
    rule_id = rule.get("rule_id") or str(uuid.uuid4())
    params = [
        {"name": "rid", "value": rule_id, "type": "STRING"},
        {"name": "cat", "value": rule.get("catalog") or "", "type": "STRING"},
        {"name": "sch", "value": rule.get("schema_name") or "", "type": "STRING"},
        {"name": "tbl", "value": rule.get("table_name") or "", "type": "STRING"},
        {"name": "rt", "value": rule.get("rule_type") or "", "type": "STRING"},
        {"name": "thr", "value": str(rule.get("threshold") or 0.0), "type": "DOUBLE"},
        {"name": "lb", "value": str(rule.get("lookback_minutes") or 60), "type": "INT"},
        {"name": "en", "value": "true" if rule.get("enabled", True) else "false", "type": "BOOLEAN"},
        {"name": "sw", "value": rule.get("slack_webhook") or "", "type": "STRING"},
        {"name": "em", "value": rule.get("email") or "", "type": "STRING"},
        {"name": "u", "value": user, "type": "STRING"},
    ]
    sql = f"""
        MERGE INTO {FQ_SCHEMA}.alert_rules t
        USING (SELECT :rid AS rule_id) s
        ON t.rule_id = s.rule_id
        WHEN MATCHED THEN UPDATE SET
            catalog=:cat, schema_name=:sch, table_name=:tbl,
            rule_type=:rt, threshold=:thr, lookback_minutes=:lb,
            enabled=:en, slack_webhook=:sw, email=:em,
            updated_at=current_timestamp()
        WHEN NOT MATCHED THEN INSERT (
            rule_id, catalog, schema_name, table_name,
            rule_type, threshold, lookback_minutes, enabled,
            slack_webhook, email, created_by, created_at, updated_at
        ) VALUES (
            :rid, :cat, :sch, :tbl,
            :rt, :thr, :lb, :en,
            :sw, :em, :u, current_timestamp(), current_timestamp()
        )
    """
    execute_sql(client, sql, parameters=params)
    return rule_id


_ALERT_RULE_READ_COLS = (
    "rule_id, catalog, schema_name, table_name, rule_type, threshold, "
    "lookback_minutes, enabled, slack_webhook, email, "
    "created_by, created_at, updated_at, "
    "last_evaluated_at, last_fired_at, last_status, last_error"
)


def alert_rule_list(user_email: Optional[str] = None) -> list[dict[str, Any]]:
    """List alert rules. If user_email given, scope to caller's rules; else all (worker)."""
    client = _sp_client()
    if user_email:
        result = execute_sql(
            client,
            f"""SELECT {_ALERT_RULE_READ_COLS}
                  FROM {FQ_SCHEMA}.alert_rules
                  WHERE created_by = :u
                  ORDER BY created_at DESC""",
            parameters=[{"name": "u", "value": user_email, "type": "STRING"}],
        )
    else:
        result = execute_sql(
            client,
            f"""SELECT {_ALERT_RULE_READ_COLS}
                  FROM {FQ_SCHEMA}.alert_rules
                  ORDER BY created_at DESC""",
        )
    return rows_as_dicts(result)


def alert_rule_get(rule_id: str) -> Optional[dict[str, Any]]:
    client = _sp_client()
    result = execute_sql(
        client,
        f"""SELECT {_ALERT_RULE_READ_COLS}
              FROM {FQ_SCHEMA}.alert_rules WHERE rule_id = :rid""",
        parameters=[{"name": "rid", "value": rule_id, "type": "STRING"}],
    )
    rows = rows_as_dicts(result)
    return rows[0] if rows else None


def alert_rule_delete(rule_id: str) -> bool:
    client = _sp_client()
    execute_sql(
        client,
        f"DELETE FROM {FQ_SCHEMA}.alert_rules WHERE rule_id = :rid",
        parameters=[{"name": "rid", "value": rule_id, "type": "STRING"}],
    )
    return True


# Columns the evaluation loop is allowed to mutate. Same protection model as
# schedule_update_run_state — keeps stray writes out of the rule definition.
_ALERT_RULE_RUN_STATE_COLS = {
    "last_evaluated_at": "TIMESTAMP",
    "last_fired_at": "TIMESTAMP",
    "last_status": "STRING",
    "last_error": "STRING",
    "enabled": "BOOLEAN",
}


def alert_rule_update_run_state(rule_id: str, fields: dict[str, Any]) -> None:
    """Patch the run-state columns of an alert rule. Used by the evaluation loop."""
    if not fields:
        return
    client = _sp_client()
    sets: list[str] = []
    params: list[dict] = [{"name": "rid", "value": rule_id, "type": "STRING"}]
    for i, (k, v) in enumerate(fields.items()):
        if k not in _ALERT_RULE_RUN_STATE_COLS:
            continue
        col_type = _ALERT_RULE_RUN_STATE_COLS[k]
        pname = f"p{i}"
        if v is None:
            sets.append(f"{k}=NULL")
            continue
        if isinstance(v, datetime):
            v = v.astimezone(timezone.utc).isoformat()
        if col_type == "BOOLEAN":
            val = "true" if bool(v) else "false"
        else:
            val = str(v)
        sets.append(f"{k}=:{pname}")
        params.append({"name": pname, "value": val, "type": col_type})
    if not sets:
        return
    sql = f"UPDATE {FQ_SCHEMA}.alert_rules SET {', '.join(sets)} WHERE rule_id = :rid"
    try:
        execute_sql(client, sql, parameters=params)
    except Exception as e:
        print(f"[alert_rule_update_run_state] warning: {e}")


def alert_rule_patch(rule_id: str, patch: dict[str, Any], user: str) -> Optional[dict[str, Any]]:
    """Apply a partial update to an existing rule, then return the new row.

    Only allows mutation of user-editable columns (everything in the rule
    definition except rule_id / created_at / created_by / run-state cols).
    Returns None if the rule doesn't exist.
    """
    existing = alert_rule_get(rule_id)
    if not existing:
        return None
    merged = dict(existing)
    editable = {
        "catalog", "schema_name", "table_name", "rule_type",
        "threshold", "lookback_minutes", "enabled",
        "slack_webhook", "email",
    }
    for k, v in (patch or {}).items():
        if k in editable:
            merged[k] = v
    merged["rule_id"] = rule_id
    alert_rule_upsert(merged, user)
    return alert_rule_get(rule_id)


# ----------------------------------------------------------------------------
# alert_events repo
# ----------------------------------------------------------------------------

_ALERT_EVENT_COLS = (
    "event_id, rule_id, fired_at, catalog, schema_name, table_name, "
    "rule_type, threshold, observed_value, message, delivery, delivery_error"
)


def alert_event_append(event: dict[str, Any]) -> str:
    """Insert an alert event row. Returns event_id."""
    client = _sp_client()
    event_id = event.get("event_id") or str(uuid.uuid4())
    sql = f"""
        INSERT INTO {FQ_SCHEMA}.alert_events
        (event_id, rule_id, fired_at, catalog, schema_name, table_name,
         rule_type, threshold, observed_value, message, delivery, delivery_error)
        VALUES (:eid, :rid, current_timestamp(), :cat, :sch, :tbl,
                :rt, :thr, :obs, :msg, :dl, :de)
    """
    try:
        execute_sql(
            client,
            sql,
            parameters=[
                {"name": "eid", "value": event_id, "type": "STRING"},
                {"name": "rid", "value": event.get("rule_id") or "", "type": "STRING"},
                {"name": "cat", "value": event.get("catalog") or "", "type": "STRING"},
                {"name": "sch", "value": event.get("schema_name") or "", "type": "STRING"},
                {"name": "tbl", "value": event.get("table_name") or "", "type": "STRING"},
                {"name": "rt", "value": event.get("rule_type") or "", "type": "STRING"},
                {"name": "thr", "value": str(event.get("threshold") or 0.0), "type": "DOUBLE"},
                {"name": "obs", "value": str(event.get("observed_value") or 0.0), "type": "DOUBLE"},
                {"name": "msg", "value": (event.get("message") or "")[:1000], "type": "STRING"},
                {"name": "dl", "value": event.get("delivery") or "", "type": "STRING"},
                {"name": "de", "value": (event.get("delivery_error") or "")[:500], "type": "STRING"},
            ],
        )
    except Exception as e:
        print(f"[alert_event_append] warning: {e}")
    return event_id


def alert_event_list(
    rule_id: Optional[str] = None,
    limit: int = 50,
    rule_ids: Optional[list[str]] = None,
) -> list[dict[str, Any]]:
    """List recent fires.

    - rule_id: filter to a single rule
    - rule_ids: filter to caller's rule_ids (no rule_id given)
    """
    client = _sp_client()
    limit = max(1, min(int(limit or 50), 500))
    if rule_id:
        result = execute_sql(
            client,
            f"""SELECT {_ALERT_EVENT_COLS}
                  FROM {FQ_SCHEMA}.alert_events
                  WHERE rule_id = :rid
                  ORDER BY fired_at DESC
                  LIMIT {limit}""",
            parameters=[{"name": "rid", "value": rule_id, "type": "STRING"}],
        )
        return rows_as_dicts(result)

    if rule_ids:
        # Inline-quoted IN list — rule_ids are UUIDs we generated; safe to
        # interpolate. If we ever take user input here, switch to NamedParameterMarker.
        safe_ids = [r.replace("'", "''") for r in rule_ids if r]
        if not safe_ids:
            return []
        in_clause = ", ".join(f"'{r}'" for r in safe_ids)
        result = execute_sql(
            client,
            f"""SELECT {_ALERT_EVENT_COLS}
                  FROM {FQ_SCHEMA}.alert_events
                  WHERE rule_id IN ({in_clause})
                  ORDER BY fired_at DESC
                  LIMIT {limit}""",
        )
        return rows_as_dicts(result)

    result = execute_sql(
        client,
        f"""SELECT {_ALERT_EVENT_COLS}
              FROM {FQ_SCHEMA}.alert_events
              ORDER BY fired_at DESC
              LIMIT {limit}""",
    )
    return rows_as_dicts(result)


def alert_event_recent_for_rule_table(
    rule_id: str, fq_table: str, since: datetime,
) -> Optional[dict[str, Any]]:
    """Return the most-recent fire for a (rule, fully-qualified-table) since `since`,
    or None. Used for wildcard-rule per-table dedupe."""
    client = _sp_client()
    cat, sch, tbl = (fq_table.split(".") + ["", "", ""])[:3]
    result = execute_sql(
        client,
        f"""SELECT {_ALERT_EVENT_COLS}
              FROM {FQ_SCHEMA}.alert_events
              WHERE rule_id = :rid AND catalog = :c AND schema_name = :s AND table_name = :t
                AND fired_at >= :since
              ORDER BY fired_at DESC
              LIMIT 1""",
        parameters=[
            {"name": "rid", "value": rule_id, "type": "STRING"},
            {"name": "c", "value": cat, "type": "STRING"},
            {"name": "s", "value": sch, "type": "STRING"},
            {"name": "t", "value": tbl, "type": "STRING"},
            {"name": "since", "value": since.astimezone(timezone.utc).isoformat(), "type": "TIMESTAMP"},
        ],
    )
    rows = rows_as_dicts(result)
    return rows[0] if rows else None


# ----------------------------------------------------------------------------
# audit repo
# ----------------------------------------------------------------------------

def audit_append(user_email: str, action: str, target: str, result: str,
                 meta: Optional[dict] = None) -> str:
    """Append an audit event. Always through SP so users don't need MODIFY."""
    client = _sp_client()
    event_id = str(uuid.uuid4())
    meta_json = json.dumps(meta or {})
    sql = f"""
        INSERT INTO {FQ_SCHEMA}.audit
        (event_id, ts, user_email, action, target, result, meta)
        VALUES (:eid, current_timestamp(), :ue, :a, :t, :r, :m)
    """
    try:
        execute_sql(
            client,
            sql,
            parameters=[
                {"name": "eid", "value": event_id, "type": "STRING"},
                {"name": "ue", "value": user_email or "unknown", "type": "STRING"},
                {"name": "a", "value": action, "type": "STRING"},
                {"name": "t", "value": target, "type": "STRING"},
                {"name": "r", "value": result, "type": "STRING"},
                {"name": "m", "value": meta_json, "type": "STRING"},
            ],
        )
    except Exception as e:
        # Never fail a user action because audit persistence hiccuped
        print(f"[audit_append] warning: {e}")
    return event_id


def audit_list(limit: int = 100) -> list[dict[str, Any]]:
    client = _sp_client()
    result = execute_sql(
        client,
        f"""SELECT event_id, ts, user_email, action, target, result, meta
              FROM {FQ_SCHEMA}.audit
              ORDER BY ts DESC
              LIMIT {int(limit)}""",
    )
    rows = rows_as_dicts(result)
    # Parse meta back to object
    for r in rows:
        m = r.get("meta")
        if isinstance(m, str):
            try:
                r["meta"] = json.loads(m)
            except Exception:
                pass
    return rows


# ----------------------------------------------------------------------------
# schedules repo (v2)
# ----------------------------------------------------------------------------

# Default columns projected by schedule_list/_get. Keeps the route layer stable.
_SCHEDULE_READ_COLS = (
    "schedule_id, catalog, schema_name, table_name, operation, "
    "cron, timezone, job_id, enabled, created_by, created_at, updated_at, "
    "schedule_type, poll_interval_seconds, min_interval_seconds, warehouse_id, "
    "user_email, last_run_at, last_checked_at, last_run_status, last_run_error, "
    "last_run_statement_id"
)


def schedule_upsert(sched: dict[str, Any], user: str) -> str:
    """Create or update a schedule. Defaults the owning user_email to `user`
    (caller email) when not explicitly provided.
    """
    client = _sp_client()
    schedule_id = sched.get("schedule_id") or str(uuid.uuid4())
    schedule_type = sched.get("schedule_type") or "cron"
    params = [
        {"name": "sid", "value": schedule_id, "type": "STRING"},
        {"name": "cat", "value": sched.get("catalog") or "", "type": "STRING"},
        {"name": "sch", "value": sched.get("schema_name") or sched.get("schema") or "", "type": "STRING"},
        {"name": "tbl", "value": sched.get("table_name") or sched.get("table") or "", "type": "STRING"},
        {"name": "op", "value": sched.get("operation") or "", "type": "STRING"},
        {"name": "cr", "value": sched.get("cron") or "", "type": "STRING"},
        {"name": "tz", "value": sched.get("timezone") or "UTC", "type": "STRING"},
        {"name": "jid", "value": sched.get("job_id") or "", "type": "STRING"},
        {"name": "en", "value": "true" if sched.get("enabled", True) else "false", "type": "BOOLEAN"},
        {"name": "st", "value": schedule_type, "type": "STRING"},
        {"name": "pi", "value": str(int(sched.get("poll_interval_seconds") or 300)), "type": "INT"},
        {"name": "mi", "value": str(int(sched.get("min_interval_seconds") or 1800)), "type": "INT"},
        {"name": "wh", "value": sched.get("warehouse_id") or "", "type": "STRING"},
        {"name": "ue", "value": sched.get("user_email") or user, "type": "STRING"},
        {"name": "u", "value": user, "type": "STRING"},
    ]
    sql = f"""
        MERGE INTO {FQ_SCHEMA}.schedules t
        USING (SELECT :sid AS schedule_id) s
        ON t.schedule_id = s.schedule_id
        WHEN MATCHED THEN UPDATE SET
            catalog=:cat, schema_name=:sch, table_name=:tbl,
            operation=:op, cron=:cr, timezone=:tz, job_id=:jid,
            enabled=:en,
            schedule_type=:st,
            poll_interval_seconds=:pi,
            min_interval_seconds=:mi,
            warehouse_id=:wh,
            user_email=:ue,
            updated_at=current_timestamp()
        WHEN NOT MATCHED THEN INSERT (
            schedule_id, catalog, schema_name, table_name,
            operation, cron, timezone, job_id, enabled,
            created_by, created_at, updated_at,
            schedule_type, poll_interval_seconds, min_interval_seconds,
            warehouse_id, user_email
        ) VALUES (
            :sid, :cat, :sch, :tbl,
            :op, :cr, :tz, :jid, :en,
            :u, current_timestamp(), current_timestamp(),
            :st, :pi, :mi, :wh, :ue
        )
    """
    execute_sql(client, sql, parameters=params)
    return schedule_id


def schedule_list(user_email: Optional[str] = None) -> list[dict[str, Any]]:
    """List schedules. If user_email is given, scope to that owner;
    otherwise return all (used by the background worker tick loop)."""
    client = _sp_client()
    if user_email:
        result = execute_sql(
            client,
            f"""SELECT {_SCHEDULE_READ_COLS}
                  FROM {FQ_SCHEMA}.schedules
                  WHERE user_email = :u
                  ORDER BY created_at DESC""",
            parameters=[{"name": "u", "value": user_email, "type": "STRING"}],
        )
    else:
        result = execute_sql(
            client,
            f"""SELECT {_SCHEDULE_READ_COLS}
                  FROM {FQ_SCHEMA}.schedules
                  ORDER BY created_at DESC""",
        )
    return rows_as_dicts(result)


def schedule_get(schedule_id: str) -> Optional[dict[str, Any]]:
    client = _sp_client()
    result = execute_sql(
        client,
        f"""SELECT {_SCHEDULE_READ_COLS}
              FROM {FQ_SCHEMA}.schedules
              WHERE schedule_id = :sid""",
        parameters=[{"name": "sid", "value": schedule_id, "type": "STRING"}],
    )
    rows = rows_as_dicts(result)
    return rows[0] if rows else None


def schedule_delete(schedule_id: str, user_email: Optional[str] = None) -> bool:
    """Delete a schedule. If user_email is provided, the row is only deleted
    when it belongs to that user — prevents cross-user deletes from the API."""
    client = _sp_client()
    if user_email:
        execute_sql(
            client,
            f"DELETE FROM {FQ_SCHEMA}.schedules WHERE schedule_id = :sid AND user_email = :u",
            parameters=[
                {"name": "sid", "value": schedule_id, "type": "STRING"},
                {"name": "u", "value": user_email, "type": "STRING"},
            ],
        )
    else:
        execute_sql(
            client,
            f"DELETE FROM {FQ_SCHEMA}.schedules WHERE schedule_id = :sid",
            parameters=[{"name": "sid", "value": schedule_id, "type": "STRING"}],
        )
    return True


# Columns the trigger loop is allowed to mutate. Anything else is a no-op —
# keeps mistakes from the worker out of the config surface.
_SCHEDULE_RUN_STATE_COLS = {
    "last_run_at": "TIMESTAMP",
    "last_checked_at": "TIMESTAMP",
    "last_run_status": "STRING",
    "last_run_error": "STRING",
    "last_run_statement_id": "STRING",
    "enabled": "BOOLEAN",
}


def schedule_update_run_state(schedule_id: str, fields: dict[str, Any]) -> None:
    """Patch the run-state columns of a schedule in place.

    Used by the trigger worker to record last_checked_at / last_run_at /
    status. Timestamps accept either ISO strings or datetime objects — we
    coerce to UTC ISO for the param list.
    """
    if not fields:
        return
    client = _sp_client()
    sets: list[str] = []
    params: list[dict] = [{"name": "sid", "value": schedule_id, "type": "STRING"}]
    for i, (k, v) in enumerate(fields.items()):
        if k not in _SCHEDULE_RUN_STATE_COLS:
            continue
        col_type = _SCHEDULE_RUN_STATE_COLS[k]
        pname = f"p{i}"
        if v is None:
            sets.append(f"{k}=NULL")
            continue
        if isinstance(v, datetime):
            v = v.astimezone(timezone.utc).isoformat()
        if col_type == "BOOLEAN":
            val = "true" if bool(v) else "false"
        else:
            val = str(v)
        sets.append(f"{k}=:{pname}")
        params.append({"name": pname, "value": val, "type": col_type})
    if not sets:
        return
    sql = f"UPDATE {FQ_SCHEMA}.schedules SET {', '.join(sets)} WHERE schedule_id = :sid"
    try:
        execute_sql(client, sql, parameters=params)
    except Exception as e:
        print(f"[schedule_update_run_state] warning: {e}")


# ----------------------------------------------------------------------------
# dashboard_configs repo — per-user saved table selections
# ----------------------------------------------------------------------------

def dashboard_save(user_email: str, name: str, tables: list[dict]) -> str:
    """Upsert a dashboard by (user_email, name). Returns config_id.

    `tables` is a list of {catalog, schema, table} dicts. Stored as JSON so we
    don't couple UC schema to the frontend shape.
    """
    if not user_email:
        raise ValueError("user_email required")
    if not name or not name.strip():
        raise ValueError("name required")
    client = _sp_client()
    tables_json = json.dumps(tables or [])
    # Is there an existing row for (user, name)? If yes, update; else insert.
    existing = execute_sql(
        client,
        f"""SELECT config_id FROM {FQ_SCHEMA}.dashboard_configs
              WHERE user_email = :u AND name = :n""",
        parameters=[
            {"name": "u", "value": user_email, "type": "STRING"},
            {"name": "n", "value": name, "type": "STRING"},
        ],
    )
    rows = existing.get("rows") or []
    if rows:
        config_id = rows[0][0]
        execute_sql(
            client,
            f"""UPDATE {FQ_SCHEMA}.dashboard_configs
                  SET tables_json = :tj, updated_at = current_timestamp()
                  WHERE config_id = :cid AND user_email = :u""",
            parameters=[
                {"name": "tj", "value": tables_json, "type": "STRING"},
                {"name": "cid", "value": config_id, "type": "STRING"},
                {"name": "u", "value": user_email, "type": "STRING"},
            ],
        )
        return config_id
    config_id = str(uuid.uuid4())
    execute_sql(
        client,
        f"""INSERT INTO {FQ_SCHEMA}.dashboard_configs
              (config_id, user_email, name, tables_json, created_at, updated_at)
              VALUES (:cid, :u, :n, :tj, current_timestamp(), current_timestamp())""",
        parameters=[
            {"name": "cid", "value": config_id, "type": "STRING"},
            {"name": "u", "value": user_email, "type": "STRING"},
            {"name": "n", "value": name, "type": "STRING"},
            {"name": "tj", "value": tables_json, "type": "STRING"},
        ],
    )
    return config_id


def dashboard_list(user_email: str) -> list[dict[str, Any]]:
    """List the caller's saved dashboards. Returns [] for unknown users."""
    if not user_email:
        return []
    client = _sp_client()
    result = execute_sql(
        client,
        f"""SELECT config_id, name, tables_json, created_at, updated_at
              FROM {FQ_SCHEMA}.dashboard_configs
              WHERE user_email = :u
              ORDER BY updated_at DESC""",
        parameters=[{"name": "u", "value": user_email, "type": "STRING"}],
    )
    rows = rows_as_dicts(result)
    # Parse tables_json back to list
    out = []
    for r in rows:
        tj = r.get("tables_json") or "[]"
        try:
            tables = json.loads(tj)
        except Exception:
            tables = []
        out.append({
            "config_id": r.get("config_id"),
            "name": r.get("name"),
            "tables": tables,
            "created_at": str(r.get("created_at") or ""),
            "updated_at": str(r.get("updated_at") or ""),
        })
    return out


def dashboard_load(user_email: str, config_id: str) -> Optional[dict[str, Any]]:
    """Return a single saved dashboard owned by `user_email`, or None."""
    if not user_email or not config_id:
        return None
    client = _sp_client()
    result = execute_sql(
        client,
        f"""SELECT config_id, name, tables_json, created_at, updated_at
              FROM {FQ_SCHEMA}.dashboard_configs
              WHERE user_email = :u AND config_id = :cid""",
        parameters=[
            {"name": "u", "value": user_email, "type": "STRING"},
            {"name": "cid", "value": config_id, "type": "STRING"},
        ],
    )
    rows = rows_as_dicts(result)
    if not rows:
        return None
    r = rows[0]
    try:
        tables = json.loads(r.get("tables_json") or "[]")
    except Exception:
        tables = []
    return {
        "config_id": r.get("config_id"),
        "name": r.get("name"),
        "tables": tables,
        "created_at": str(r.get("created_at") or ""),
        "updated_at": str(r.get("updated_at") or ""),
    }


def dashboard_delete(user_email: str, config_id: str) -> bool:
    """Delete a dashboard, scoped to the caller."""
    if not user_email or not config_id:
        return False
    client = _sp_client()
    execute_sql(
        client,
        f"""DELETE FROM {FQ_SCHEMA}.dashboard_configs
              WHERE user_email = :u AND config_id = :cid""",
        parameters=[
            {"name": "u", "value": user_email, "type": "STRING"},
            {"name": "cid", "value": config_id, "type": "STRING"},
        ],
    )
    return True


# ----------------------------------------------------------------------------
# card_cache repo — per-user per-table cached payload
# ----------------------------------------------------------------------------

def card_cache_get(user_email: str, catalog: str, schema: str, table: str) -> Optional[dict]:
    """Return the cached payload for (user, target table), or None."""
    if not user_email:
        return None
    client = _sp_client()
    result = execute_sql(
        client,
        f"""SELECT payload_json, updated_at
              FROM {FQ_SCHEMA}.card_cache
              WHERE user_email = :u AND catalog = :c
                AND schema_name = :s AND table_name = :t
              LIMIT 1""",
        parameters=[
            {"name": "u", "value": user_email, "type": "STRING"},
            {"name": "c", "value": catalog, "type": "STRING"},
            {"name": "s", "value": schema, "type": "STRING"},
            {"name": "t", "value": table, "type": "STRING"},
        ],
    )
    rows = rows_as_dicts(result)
    if not rows:
        return None
    r = rows[0]
    try:
        payload = json.loads(r.get("payload_json") or "{}")
    except (ValueError, TypeError):
        payload = {}
    return {
        "payload": payload,
        "updated_at": str(r.get("updated_at") or ""),
    }


def card_cache_save(user_email: str, catalog: str, schema: str, table: str, payload: dict) -> None:
    """Upsert the cached payload. Silently no-ops if user_email missing."""
    if not user_email:
        return
    client = _sp_client()
    body = json.dumps(payload)
    sql = f"""
        MERGE INTO {FQ_SCHEMA}.card_cache t
        USING (SELECT :u AS user_email, :c AS catalog, :s AS schema_name,
                      :tbl AS table_name, :p AS payload_json) src
        ON t.user_email = src.user_email
           AND t.catalog = src.catalog
           AND t.schema_name = src.schema_name
           AND t.table_name = src.table_name
        WHEN MATCHED THEN UPDATE SET
            payload_json = src.payload_json,
            updated_at = current_timestamp()
        WHEN NOT MATCHED THEN INSERT
            (user_email, catalog, schema_name, table_name, payload_json, updated_at)
            VALUES (src.user_email, src.catalog, src.schema_name,
                    src.table_name, src.payload_json, current_timestamp())
    """
    execute_sql(
        client,
        sql,
        parameters=[
            {"name": "u", "value": user_email, "type": "STRING"},
            {"name": "c", "value": catalog, "type": "STRING"},
            {"name": "s", "value": schema, "type": "STRING"},
            {"name": "tbl", "value": table, "type": "STRING"},
            {"name": "p", "value": body, "type": "STRING"},
        ],
    )
