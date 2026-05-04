"""Action buttons: OPTIMIZE, VACUUM, PO toggle, scheduling.

Every action writes to the UC-backed audit log via db.audit_append (SP-owned).
Long-running ops still use synchronous Statement Execution for v1; swap to
Jobs API for PB-scale tables.
"""
from typing import Literal, Optional

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, field_validator

from .. import db
from ..config import get_user_client
from ..sql_client import InvalidIdentifier, execute_sql, validate_ident

router = APIRouter(prefix="/api/actions", tags=["actions"])


def _user_client(x_forwarded_access_token: Optional[str] = Header(default=None)):
    return get_user_client(x_forwarded_access_token)


def _audit(user: str, action: str, target: str, result: str, meta: dict | None = None):
    db.audit_append(user, action, target, result, meta)


class TableRef(BaseModel):
    catalog: str
    schema: str
    table: str

    @field_validator("catalog", "schema", "table")
    @classmethod
    def _check_ident(cls, v: str, info) -> str:
        try:
            return validate_ident(v, info.field_name)
        except InvalidIdentifier as e:
            raise ValueError(str(e)) from e


class VacuumRequest(TableRef):
    mode: Literal["LITE", "FULL"] = "LITE"


class ToggleRequest(TableRef):
    enabled: bool


class ScheduleRequest(TableRef):
    operation: Literal["OPTIMIZE", "VACUUM_LITE", "VACUUM_FULL"]
    cron: str  # e.g. "0 2 * * *"
    timezone_id: str = "UTC"


@router.get("/audit")
def get_audit(limit: int = 100):
    try:
        entries = db.audit_list(limit=limit)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    # Map DB shape (ts/user_email) to the API shape frontend expects (ts/user).
    out = []
    for e in entries:
        out.append({
            "ts": str(e.get("ts") or ""),
            "user": e.get("user_email"),
            "action": e.get("action"),
            "target": e.get("target"),
            "result": e.get("result"),
            "meta": e.get("meta") or {},
        })
    return {"entries": out}


@router.post("/optimize")
def run_optimize(
    req: TableRef,
    x_forwarded_email: Optional[str] = Header(default=None),
    client=Depends(_user_client),
):
    """Run OPTIMIZE synchronously via Statement Execution.

    STATUS: wired to SQL, not to Jobs API. For long-running OPTIMIZE on PB-scale
    tables, swap to Jobs API so we don't block the HTTP request.
    TODO: Use jobs.submit + run_now for async, return run_id, poll from UI.
    """
    full = f"`{req.catalog}`.`{req.schema}`.`{req.table}`"
    user = x_forwarded_email or "unknown"
    try:
        # fire-and-forget: submit to Statement Execution and return immediately.
        # OPTIMIZE typically takes minutes; we don't want to block the HTTP req.
        from ..config import get_warehouse_id as _gwh
        wh_used = _gwh()
        client_kind = "user" if (x_forwarded_email) else "sp"
        res = execute_sql(client, f"OPTIMIZE {full}", wait_timeout="5s", fire_and_forget=True)
        _audit(user, "OPTIMIZE", full, "submitted", {
            "statement_id": res.get("statement_id"),
            "state": res.get("state"),
            "warehouse_id": wh_used,
            "client_kind": client_kind,
        })
        return {
            "status": "done" if res.get("done") else "submitted",
            "target": full,
            "statement_id": res.get("statement_id"),
            "state": res.get("state"),
            "done": res.get("done", False),
        }
    except Exception as e:
        _audit(user, "OPTIMIZE", full, "error", {"error": str(e)})
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/vacuum")
def run_vacuum(
    req: VacuumRequest,
    x_forwarded_email: Optional[str] = Header(default=None),
    client=Depends(_user_client),
):
    """Run VACUUM LITE or FULL.

    STATUS: stubbed SQL. VACUUM FULL on PB tables is expensive — UI must show
    confirm modal (frontend already does). TODO: move to Jobs API.
    """
    full = f"`{req.catalog}`.`{req.schema}`.`{req.table}`"
    user = x_forwarded_email or "unknown"
    # LITE vs FULL differ in retention / file cleanup scope. Exact syntax varies
    # by DBR version; verify against target workspace before shipping.
    sql = f"VACUUM {full} " + ("FULL" if req.mode == "FULL" else "LITE")
    try:
        from ..config import get_warehouse_id as _gwh
        wh_used = _gwh()
        client_kind = "user" if (x_forwarded_email) else "sp"
        res = execute_sql(client, sql, wait_timeout="5s", fire_and_forget=True)
        _audit(user, f"VACUUM_{req.mode}", full, "submitted", {
            "statement_id": res.get("statement_id"),
            "state": res.get("state"),
            "warehouse_id": wh_used,
            "client_kind": client_kind,
        })
        return {
            "status": "done" if res.get("done") else "submitted",
            "target": full,
            "mode": req.mode,
            "statement_id": res.get("statement_id"),
            "state": res.get("state"),
            "done": res.get("done", False),
        }
    except Exception as e:
        _audit(user, f"VACUUM_{req.mode}", full, "error", {
            "error": str(e),
            "warehouse_id": wh_used if 'wh_used' in locals() else None,
            "client_kind": client_kind if 'client_kind' in locals() else None,
            "x_forwarded_email": x_forwarded_email,
        })
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/toggle_po")
def toggle_po(
    req: ToggleRequest,
    x_forwarded_email: Optional[str] = Header(default=None),
    client=Depends(_user_client),
):
    """Enable/disable PO on a UC managed table.

    Uses the canonical UC syntax (ALTER TABLE ... ENABLE|DISABLE PREDICTIVE OPTIMIZATION)
    which respects schema / catalog inheritance. For INHERIT behavior, pass
    enabled=null via a separate endpoint (not wired here).
    """
    full = f"`{req.catalog}`.`{req.schema}`.`{req.table}`"
    user = x_forwarded_email or "unknown"
    sql = f"ALTER TABLE {full} {'ENABLE' if req.enabled else 'DISABLE'} PREDICTIVE OPTIMIZATION"
    try:
        execute_sql(client, sql)
        _audit(user, "TOGGLE_PO", full, "ok", {"enabled": req.enabled})
        return {"status": "ok", "target": full, "enabled": req.enabled}
    except Exception as e:
        _audit(user, "TOGGLE_PO", full, "error", {"error": str(e)})
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/force_trigger")
def force_trigger(
    req: TableRef,
    x_forwarded_email: Optional[str] = Header(default=None),
    client=Depends(_user_client),
):
    """Force PO to run outside its normal cadence.

    There is no public SQL command for forcing the PO scheduler itself.
    Stand-in: submit OPTIMIZE + VACUUM LITE as the OBO user — these are the
    same operations PO would run. Both submitted in parallel via threads
    so the HTTP request returns in ~5s rather than ~10s sequential.
    """
    from concurrent.futures import ThreadPoolExecutor
    from ..config import WAREHOUSE_OVERRIDE, get_warehouse_id as _gwh

    full = f"`{req.catalog}`.`{req.schema}`.`{req.table}`"
    user = x_forwarded_email or "unknown"
    try:
        wh_used = _gwh()
    except Exception as e:
        _audit(user, "FORCE_PO", full, "error",
               {"error": f"warehouse lookup: {e}",
                "client_kind": "user" if x_forwarded_email else "sp"})
        raise HTTPException(status_code=500, detail=f"warehouse lookup failed: {e}")
    client_kind = "user" if x_forwarded_email else "sp"

    submitted: dict[str, dict] = {}
    errors: dict[str, str] = {}

    statements = (
        ("OPTIMIZE", f"OPTIMIZE {full}"),
        ("VACUUM_LITE", f"VACUUM {full} LITE"),
    )

    # Snapshot the warehouse override and re-set it in each worker. We can't
    # share a single Context object across threads (Context.run raises if
    # entered concurrently). Each worker reads/sets WAREHOUSE_OVERRIDE on its
    # own thread's context.
    wh_override = WAREHOUSE_OVERRIDE.get()

    def _submit(label_sql):
        label, sql = label_sql
        token = WAREHOUSE_OVERRIDE.set(wh_override) if wh_override is not None else None
        try:
            res = execute_sql(client, sql, wait_timeout="5s", fire_and_forget=True)
            return label, {
                "statement_id": res.get("statement_id"),
                "state": res.get("state"),
                "done": res.get("done", False),
            }, None
        except Exception as e:
            return label, None, str(e)
        finally:
            if token is not None:
                WAREHOUSE_OVERRIDE.reset(token)

    with ThreadPoolExecutor(max_workers=len(statements)) as ex:
        for label, ok, err in ex.map(_submit, statements):
            if ok is not None:
                submitted[label] = ok
            if err is not None:
                errors[label] = err

    audit_meta = {
        "submitted": submitted,
        "errors": errors or None,
        "warehouse_id": wh_used,
        "client_kind": client_kind,
    }
    if errors and not submitted:
        _audit(user, "FORCE_PO", full, "error", audit_meta)
        raise HTTPException(status_code=500, detail=f"All sub-statements failed: {errors}")

    _audit(user, "FORCE_PO", full,
           "submitted" if not errors else "partial",
           audit_meta)
    return {
        "status": "submitted" if not errors else "partial",
        "target": full,
        "note": "PO scheduler has no public force endpoint; submitted equivalent OPTIMIZE + VACUUM LITE as a stand-in.",
        "submitted": submitted,
        "errors": errors or None,
    }


@router.post("/schedule")
def schedule_maintenance(
    req: ScheduleRequest,
    x_forwarded_email: Optional[str] = Header(default=None),
    client=Depends(_user_client),
):
    """Legacy compatibility shim — forwards to /api/schedules.

    Older frontend builds call this endpoint. Rather than breaking them at
    deploy time, we translate the body to the new schedules module and
    persist the same row the new /api/schedules endpoint would write.
    """
    user = x_forwarded_email or "unknown"
    if not x_forwarded_email:
        raise HTTPException(status_code=401, detail="user email header missing")
    if req.operation not in {"OPTIMIZE", "VACUUM_LITE", "VACUUM_FULL"}:
        raise HTTPException(status_code=400, detail="invalid operation")
    if not req.cron or len(req.cron.split()) != 5:
        raise HTTPException(status_code=400, detail="cron must be 5-field")

    from ..config import get_warehouse_id as _gwh
    wh = _gwh() or ""
    payload = {
        "catalog": req.catalog,
        "schema_name": req.schema,
        "table_name": req.table,
        "operation": req.operation,
        "schedule_type": "cron",
        "cron": req.cron,
        "timezone": req.timezone_id or "UTC",
        "poll_interval_seconds": 300,
        "min_interval_seconds": 1800,
        "warehouse_id": wh,
        "enabled": True,
        "user_email": x_forwarded_email,
    }
    try:
        sid = db.schedule_upsert(payload, x_forwarded_email)
        _audit(user, f"SCHEDULE_{req.operation}",
               f"{req.catalog}.{req.schema}.{req.table}",
               "ok", {"schedule_id": sid, "cron": req.cron, "via": "legacy_shim"})
    except Exception as e:
        _audit(user, f"SCHEDULE_{req.operation}",
               f"{req.catalog}.{req.schema}.{req.table}",
               "error", {"error": str(e), "via": "legacy_shim"})
        raise HTTPException(status_code=500, detail=str(e))
    return {"ok": True, "schedule_id": sid, "target": f"{req.catalog}.{req.schema}.{req.table}"}
