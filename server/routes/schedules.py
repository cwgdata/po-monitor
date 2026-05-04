"""Schedules CRUD + manual-run endpoint.

Schedules are per-user (`user_email` column). A schedule can be one of:
  - cron    : fires on a cron expression in UTC (or arbitrary tz)
  - trigger : polls DESCRIBE HISTORY on the target, fires on new data commits

The background worker in server/scheduler.py actually fires them. All rows
are persisted in `${PO_MONITOR_CATALOG}.po_monitor.schedules`.

Compatibility: the legacy /api/actions/schedule endpoint is kept as a shim
(see server/routes/actions.py) that translates to this module.
"""
from typing import Literal, Optional

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, Field

from .. import db

router = APIRouter(prefix="/api/schedules", tags=["schedules"])


ALLOWED_OPS = {"OPTIMIZE", "VACUUM_LITE", "VACUUM_FULL"}
ALLOWED_TYPES = {"cron", "trigger"}


class ScheduleBody(BaseModel):
    # Field is named `schema_name` (trailing underscore would shadow BaseModel
    # internals) but the JSON shape uses `schema` to match the rest of the
    # app. Pydantic v2 + populate_by_name accepts both.
    catalog: str
    schema_name: str = Field(..., alias="schema")
    table: str
    operation: Literal["OPTIMIZE", "VACUUM_LITE", "VACUUM_FULL"]
    schedule_type: Literal["cron", "trigger"] = "cron"
    cron: Optional[str] = None
    timezone: Optional[str] = "UTC"
    poll_interval_seconds: Optional[int] = 300
    min_interval_seconds: Optional[int] = 1800
    warehouse_id: Optional[str] = None
    enabled: bool = True

    model_config = {"populate_by_name": True}


class SchedulePatch(BaseModel):
    enabled: Optional[bool] = None
    cron: Optional[str] = None
    timezone: Optional[str] = None
    poll_interval_seconds: Optional[int] = None
    min_interval_seconds: Optional[int] = None
    warehouse_id: Optional[str] = None
    operation: Optional[Literal["OPTIMIZE", "VACUUM_LITE", "VACUUM_FULL"]] = None
    schedule_type: Optional[Literal["cron", "trigger"]] = None
    # Target (catalog/schema/table) is editable too, in case the user wants to
    # repoint a schedule. The /{id} PATCH revalidates fully through _validate_body.
    catalog: Optional[str] = None
    schema_name: Optional[str] = Field(default=None, alias="schema")
    table: Optional[str] = None

    model_config = {"populate_by_name": True}


def _validate_body(b: ScheduleBody) -> None:
    """Per-type validation — keeps the DB from growing weird half-configured rows."""
    if b.operation not in ALLOWED_OPS:
        raise HTTPException(status_code=400, detail=f"operation must be one of {sorted(ALLOWED_OPS)}")
    if b.schedule_type == "cron":
        if not b.cron or len(b.cron.split()) != 5:
            raise HTTPException(status_code=400, detail="cron must be a 5-field expression (e.g. '0 2 * * *')")
    else:
        if (b.poll_interval_seconds or 0) < 15:
            raise HTTPException(status_code=400, detail="poll_interval_seconds must be >= 15")
        if (b.min_interval_seconds or 0) < 60:
            raise HTTPException(status_code=400, detail="min_interval_seconds must be >= 60")
    if not b.warehouse_id:
        raise HTTPException(status_code=400, detail="warehouse_id required")


def _row_to_api(r: dict) -> dict:
    """Shape for frontend — renames schema_name to schema and fills in defaults."""
    return {
        "schedule_id": r.get("schedule_id"),
        "catalog": r.get("catalog"),
        "schema": r.get("schema_name"),
        "table": r.get("table_name"),
        "operation": r.get("operation"),
        "schedule_type": r.get("schedule_type") or "cron",
        "cron": r.get("cron"),
        "timezone": r.get("timezone") or "UTC",
        "poll_interval_seconds": r.get("poll_interval_seconds"),
        "min_interval_seconds": r.get("min_interval_seconds"),
        "warehouse_id": r.get("warehouse_id"),
        "enabled": bool(r.get("enabled")) if r.get("enabled") is not None else False,
        "user_email": r.get("user_email") or r.get("created_by"),
        "created_at": str(r.get("created_at") or ""),
        "updated_at": str(r.get("updated_at") or ""),
        "last_run_at": str(r.get("last_run_at") or "") or None,
        "last_checked_at": str(r.get("last_checked_at") or "") or None,
        "last_run_status": r.get("last_run_status"),
        "last_run_error": r.get("last_run_error"),
        "last_run_statement_id": r.get("last_run_statement_id"),
    }


@router.get("")
def list_schedules(x_forwarded_email: Optional[str] = Header(default=None)):
    try:
        rows = db.schedule_list(user_email=x_forwarded_email)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    return {"schedules": [_row_to_api(r) for r in rows]}


@router.post("")
def create_schedule(
    body: ScheduleBody,
    x_forwarded_email: Optional[str] = Header(default=None),
):
    if not x_forwarded_email:
        raise HTTPException(status_code=401, detail="user email header missing")
    _validate_body(body)
    payload = {
        "catalog": body.catalog,
        "schema_name": body.schema_name,
        "table_name": body.table,
        "operation": body.operation,
        "schedule_type": body.schedule_type,
        "cron": body.cron or "",
        "timezone": body.timezone or "UTC",
        "poll_interval_seconds": body.poll_interval_seconds or 300,
        "min_interval_seconds": body.min_interval_seconds or 1800,
        "warehouse_id": body.warehouse_id or "",
        "enabled": body.enabled,
        "user_email": x_forwarded_email,
    }
    try:
        sid = db.schedule_upsert(payload, x_forwarded_email)
        db.audit_append(
            x_forwarded_email, f"SCHEDULE_{body.operation}_CREATE",
            f"{body.catalog}.{body.schema_name}.{body.table}", "ok",
            {"schedule_id": sid, "type": body.schedule_type},
        )
    except Exception as e:
        db.audit_append(
            x_forwarded_email, f"SCHEDULE_{body.operation}_CREATE",
            f"{body.catalog}.{body.schema_name}.{body.table}", "error",
            {"error": str(e)},
        )
        raise HTTPException(status_code=500, detail=str(e))
    saved = db.schedule_get(sid)
    return _row_to_api(saved or payload | {"schedule_id": sid})


@router.patch("/{schedule_id}")
def update_schedule(
    schedule_id: str,
    patch: SchedulePatch,
    x_forwarded_email: Optional[str] = Header(default=None),
):
    if not x_forwarded_email:
        raise HTTPException(status_code=401, detail="user email header missing")
    existing = db.schedule_get(schedule_id)
    if not existing:
        raise HTTPException(status_code=404, detail="schedule not found")
    if existing.get("user_email") and existing.get("user_email") != x_forwarded_email:
        raise HTTPException(status_code=403, detail="not your schedule")

    # Merge patch into existing row, re-validate, upsert through the same path.
    merged = dict(existing)
    for k, v in patch.model_dump(exclude_none=True).items():
        merged[k] = v
    # schedule_upsert expects `schema_name`, not `schema`.
    payload = {
        "schedule_id": schedule_id,
        "catalog": merged.get("catalog"),
        "schema_name": merged.get("schema_name"),
        "table_name": merged.get("table_name"),
        "operation": merged.get("operation"),
        "schedule_type": merged.get("schedule_type") or "cron",
        "cron": merged.get("cron") or "",
        "timezone": merged.get("timezone") or "UTC",
        "poll_interval_seconds": merged.get("poll_interval_seconds") or 300,
        "min_interval_seconds": merged.get("min_interval_seconds") or 1800,
        "warehouse_id": merged.get("warehouse_id") or "",
        "enabled": bool(merged.get("enabled")),
        "user_email": merged.get("user_email") or x_forwarded_email,
    }
    try:
        db.schedule_upsert(payload, x_forwarded_email)
        db.audit_append(
            x_forwarded_email, "SCHEDULE_UPDATE", schedule_id, "ok",
            {"patch": patch.model_dump(exclude_none=True)},
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    return _row_to_api(db.schedule_get(schedule_id) or payload)


@router.delete("/{schedule_id}")
def delete_schedule(
    schedule_id: str,
    x_forwarded_email: Optional[str] = Header(default=None),
):
    if not x_forwarded_email:
        raise HTTPException(status_code=401, detail="user email header missing")
    try:
        db.schedule_delete(schedule_id, user_email=x_forwarded_email)
        db.audit_append(x_forwarded_email, "SCHEDULE_DELETE", schedule_id, "ok")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    return {"ok": True}


@router.post("/{schedule_id}/run")
def run_schedule_now(
    schedule_id: str,
    x_forwarded_email: Optional[str] = Header(default=None),
):
    """Fire a schedule once, right now, regardless of cron/trigger state.

    Runs through the same pipeline the worker uses (SP-authed SQL), so the
    user doesn't need to have MODIFY on the target table — the app SP does.
    """
    if not x_forwarded_email:
        raise HTTPException(status_code=401, detail="user email header missing")
    sched = db.schedule_get(schedule_id)
    if not sched:
        raise HTTPException(status_code=404, detail="schedule not found")
    if sched.get("user_email") and sched.get("user_email") != x_forwarded_email:
        raise HTTPException(status_code=403, detail="not your schedule")
    from ..scheduler import fire_schedule  # import here to avoid worker->routes cycle at boot
    try:
        res = fire_schedule(sched, manual=True, actor=x_forwarded_email)
    except Exception as e:
        db.audit_append(
            x_forwarded_email, "SCHEDULE_RUN_MANUAL", schedule_id, "error",
            {"error": str(e)},
        )
        raise HTTPException(status_code=500, detail=str(e))
    return res
