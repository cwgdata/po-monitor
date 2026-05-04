"""Dual-mode auth + config for PO Monitor.

In Databricks Apps:
- Service principal creds auto-injected (DATABRICKS_CLIENT_ID/SECRET)
- User token available in X-Forwarded-Access-Token header (OBO)

Locally:
- Falls back to databricks-cli profile or env vars
"""
import contextvars
import os
from typing import Optional
from databricks.sdk import WorkspaceClient

IS_DATABRICKS_APP = bool(os.environ.get("DATABRICKS_APP_NAME"))

# Per-request override for which SQL warehouse queries run on. Set by the
# warehouse-header middleware from the X-Warehouse-Id request header.
WAREHOUSE_OVERRIDE: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "warehouse_override", default=None,
)


def get_workspace_host() -> str:
    """Get workspace host URL with https:// prefix."""
    if IS_DATABRICKS_APP:
        host = os.environ.get("DATABRICKS_HOST", "")
        if host and not host.startswith("http"):
            host = f"https://{host}"
        return host
    profile = os.environ.get("DATABRICKS_PROFILE")
    w = WorkspaceClient(profile=profile) if profile else WorkspaceClient()
    return w.config.host


def get_warehouse_id() -> Optional[str]:
    """Warehouse ID: request-scoped override first, then env (resource binding).

    The override is set from the X-Warehouse-Id request header so every query
    runs on whatever warehouse the user picked in the sidebar dropdown.
    """
    override = WAREHOUSE_OVERRIDE.get()
    if override:
        return override
    return os.environ.get("DATABRICKS_WAREHOUSE_ID")


def get_app_client() -> WorkspaceClient:
    """WorkspaceClient authed as service principal (app identity)."""
    if IS_DATABRICKS_APP:
        return WorkspaceClient()
    profile = os.environ.get("DATABRICKS_PROFILE")
    return WorkspaceClient(profile=profile) if profile else WorkspaceClient()


def preflight_sp_warehouse_access() -> tuple[bool, str]:
    """Verify the app SP can use the configured default warehouse.

    Background loops (scheduler, alerts engine) run as the SP and will silently
    fail with `Config: ... auth_type=oauth-m2m ... You do not have permission
    to use the SQL Warehouse` if the SP is missing CAN_USE. This runs at app
    startup so the failure surfaces in logs once instead of as runtime 500s.

    Returns (ok, message). Caller decides whether to log/block.
    """
    wh = os.environ.get("DATABRICKS_WAREHOUSE_ID")
    if not wh:
        return True, "no DATABRICKS_WAREHOUSE_ID configured (skip preflight)"
    try:
        client = get_app_client()
        # Cheap noop that fails immediately with PERMISSION_DENIED if the SP
        # lacks CAN_USE. Don't wait — we just want to see whether submit is
        # accepted.
        client.statement_execution.execute_statement(
            statement="SELECT 1",
            warehouse_id=wh,
            wait_timeout="5s",
        )
        return True, f"SP CAN_USE warehouse {wh}"
    except Exception as e:  # noqa: BLE001
        msg = str(e)
        if "permission" in msg.lower() or "do not have permission" in msg.lower():
            return False, (
                f"SP missing CAN_USE on warehouse {wh}. Background loops will fail. "
                f"Fix: databricks api put /api/2.0/permissions/warehouses/{wh} "
                f"--json '{{\"access_control_list\":[{{\"service_principal_name\":\"<sp>\","
                f"\"permission_level\":\"CAN_USE\"}}]}}'"
            )
        return False, f"warehouse preflight error (non-permission): {msg}"


def get_user_client(user_token: Optional[str]) -> WorkspaceClient:
    """WorkspaceClient authed on-behalf-of the logged-in user.

    Uses the X-Forwarded-Access-Token header injected by Databricks Apps.
    Falls back to app identity if no user token (local dev).

    NOTE: force `auth_type='pat'` so the SDK doesn't fall back to the app's
    OAuth M2M env vars (DATABRICKS_CLIENT_ID/SECRET) and error with
    "more than one authorization method configured". The Databricks-issued
    OBO access token works as a bearer token the same way a PAT does.
    """
    if user_token:
        return WorkspaceClient(
            host=get_workspace_host(),
            token=user_token,
            auth_type="pat",
        )
    return get_app_client()


# Alert / threshold defaults — override via Config page (persisted in UC).
DEFAULT_THRESHOLDS = {
    "optimize_amber_days": 7,
    "optimize_red_days": 14,
    "vacuum_amber_days": 14,
    "vacuum_red_days": 30,
    "unclustered_amber_pct": 0.20,
    "file_size_drop_amber_pct": 0.15,
    "optimize_failure_rate_amber": 0.10,
    "optimize_failure_rate_red": 0.30,
    "merge_conflict_rate_amber": 0.10,
    "merge_conflict_rate_red": 0.30,
    "merge_window_hours": 24,
    "auto_refresh_seconds": 300,
}
