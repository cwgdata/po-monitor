"""App config — UC-backed key/value persistence.

All reads + writes go through the app service principal (see server/db.py
for the full rationale). The workspace host + warehouse ID are still
sourced from the environment (they come from the app resource binding).

API shape is unchanged from v1 so the frontend AppConfig type still works:
  GET    /api/config                 → AppConfig JSON
  PATCH  /api/config                 → apply patch, return AppConfig
  GET    /api/config/warehouses      → warehouses the caller can USE
"""
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel

from .. import db
from ..config import (
    DEFAULT_THRESHOLDS,
    IS_DATABRICKS_APP,
    get_user_client,
    get_warehouse_id,
    get_workspace_host,
)

router = APIRouter(prefix="/api/config", tags=["config"])


# Keys managed as top-level AppConfig fields (distinct from threshold keys).
_TOP_LEVEL_KEYS = (
    "default_catalog",
    "default_schema",
    "slack_webhook_url",
    "alert_email_to",
    "warehouse_id_override",
)
_THRESHOLD_KEYS = tuple(DEFAULT_THRESHOLDS.keys())


class ConfigPatch(BaseModel):
    default_catalog: Optional[str] = None
    default_schema: Optional[str] = None
    slack_webhook_url: Optional[str] = None
    alert_email_to: Optional[str] = None
    warehouse_id_override: Optional[str] = None
    thresholds: Optional[dict] = None


def _user_client(x_forwarded_access_token: Optional[str] = Header(default=None)):
    return get_user_client(x_forwarded_access_token)


def _coerce_threshold(raw: Optional[str], default):
    if raw is None or raw == "":
        return default
    try:
        return float(raw) if isinstance(default, float) else int(float(raw))
    except (TypeError, ValueError):
        return default


def _compose_config(user_email: Optional[str] = None) -> dict:
    """Read UC config table and compose the AppConfig JSON shape.

    Per-user rows (user_email = caller) override global defaults (user_email IS NULL).
    """
    try:
        kv = db.config_list(user_email=user_email)
    except Exception as e:
        # Bootstrap may have failed; fall back to defaults in memory so the
        # UI still renders something rather than 500'ing.
        print(f"[config] config_list failed: {e}")
        kv = {}

    thresholds = {
        k: _coerce_threshold(kv.get(k), v) for k, v in DEFAULT_THRESHOLDS.items()
    }

    warehouse_override = kv.get("warehouse_id_override") or None
    warehouse = warehouse_override or get_warehouse_id()

    return {
        "workspace_host": get_workspace_host(),
        "warehouse_id": warehouse,
        "warehouse_id_override": warehouse_override,
        "is_databricks_app": IS_DATABRICKS_APP,
        "default_catalog": kv.get("default_catalog") or None,
        "default_schema": kv.get("default_schema") or None,
        "slack_webhook_url": kv.get("slack_webhook_url") or None,
        "alert_email_to": kv.get("alert_email_to") or None,
        "thresholds": thresholds,
    }


@router.get("")
def get_config(x_forwarded_email: Optional[str] = Header(default=None)):
    return _compose_config(user_email=x_forwarded_email)


@router.patch("")
def update_config(
    patch: ConfigPatch,
    x_forwarded_email: Optional[str] = Header(default=None),
):
    user = x_forwarded_email or "unknown"
    payload = patch.model_dump(exclude_none=True)

    # 1. Top-level scalar fields
    to_set: dict[str, str] = {}
    for k in _TOP_LEVEL_KEYS:
        if k in payload:
            to_set[k] = payload[k]

    # 2. Thresholds (nested dict) — only whitelisted keys
    thresholds = payload.get("thresholds") or {}
    for k in _THRESHOLD_KEYS:
        if k in thresholds:
            to_set[k] = thresholds[k]

    if to_set:
        try:
            # Per-user writes — writes go to the caller's rows, not global
            db.config_set_many(to_set, user, user_email=x_forwarded_email)
            db.audit_append(user, "CONFIG_UPDATE", "config",
                            "ok", {"keys": list(to_set.keys()), "scope": "user"})
        except Exception as e:
            db.audit_append(user, "CONFIG_UPDATE", "config", "error",
                            {"error": str(e)})
            raise HTTPException(status_code=500, detail=f"Config write failed: {e}")

    return _compose_config(user_email=x_forwarded_email)


@router.get("/warehouses")
def list_warehouses(client=Depends(_user_client)):
    """List SQL warehouses the logged-in user can see.

    Uses OBO so we show only the warehouses the user has access to.
    """
    try:
        warehouses = []
        for wh in client.warehouses.list():
            warehouses.append({
                "id": wh.id,
                "name": wh.name,
                "state": wh.state.value if wh.state else None,
                "size": wh.cluster_size,
            })
        return {"warehouses": warehouses}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
