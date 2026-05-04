"""Saved dashboards — per-user table selections.

All rows are scoped by user_email (the X-Forwarded-Email header). Users can
only see, create, load, or delete their own configs. Writes are persisted
through the SP (see server/db.py rationale) but every query filters by the
caller's email so no cross-user access is possible from the API.
"""
from typing import Optional

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

from .. import db

router = APIRouter(prefix="/api/dashboards", tags=["dashboards"])


class DashboardSave(BaseModel):
    # We accept a loose list[dict] so the JSON key `schema` doesn't collide
    # with Pydantic v2's BaseModel.schema method. Validation happens in
    # _clean_tables below.
    name: str
    tables: list[dict]


def _clean_tables(tables: list[dict]) -> list[dict]:
    """Drop bad entries and keep only the {catalog, schema, table} triple."""
    out: list[dict] = []
    for t in tables or []:
        if not isinstance(t, dict):
            continue
        cat = (t.get("catalog") or "").strip()
        sch = (t.get("schema") or "").strip()
        tbl = (t.get("table") or "").strip()
        if cat and sch and tbl:
            out.append({"catalog": cat, "schema": sch, "table": tbl})
    return out


@router.get("")
def list_dashboards(x_forwarded_email: Optional[str] = Header(default=None)):
    """List the caller's saved dashboards. Empty list if caller is unknown."""
    if not x_forwarded_email:
        # In local dev (no OBO headers) the frontend still works; just show
        # an empty list rather than 400'ing.
        return {"configs": []}
    try:
        configs = db.dashboard_list(x_forwarded_email)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    return {"configs": configs}


@router.post("")
def save_dashboard(
    body: DashboardSave,
    x_forwarded_email: Optional[str] = Header(default=None),
):
    if not x_forwarded_email:
        raise HTTPException(status_code=401, detail="user email header missing")
    name = (body.name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="name required")
    cleaned = _clean_tables(body.tables)
    try:
        config_id = db.dashboard_save(x_forwarded_email, name, cleaned)
        db.audit_append(
            x_forwarded_email, "DASHBOARD_SAVE", config_id, "ok",
            {"name": name, "table_count": len(cleaned)},
        )
    except Exception as e:
        db.audit_append(
            x_forwarded_email, "DASHBOARD_SAVE", name, "error",
            {"error": str(e)},
        )
        raise HTTPException(status_code=500, detail=str(e))
    return {"ok": True, "config_id": config_id, "name": name, "tables": cleaned}


@router.post("/{config_id}/load")
def load_dashboard(
    config_id: str,
    x_forwarded_email: Optional[str] = Header(default=None),
):
    """Return the config so the frontend can apply it to its selection state.

    We use POST here (not GET) because load is the action the user explicitly
    invokes from the sidebar — it maps to an audit row. A future version could
    also return a short-lived share URL.
    """
    if not x_forwarded_email:
        raise HTTPException(status_code=401, detail="user email header missing")
    try:
        cfg = db.dashboard_load(x_forwarded_email, config_id)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    if not cfg:
        raise HTTPException(status_code=404, detail="dashboard not found")
    db.audit_append(
        x_forwarded_email, "DASHBOARD_LOAD", config_id, "ok",
        {"name": cfg.get("name"), "table_count": len(cfg.get("tables") or [])},
    )
    return cfg


@router.delete("/{config_id}")
def delete_dashboard(
    config_id: str,
    x_forwarded_email: Optional[str] = Header(default=None),
):
    if not x_forwarded_email:
        raise HTTPException(status_code=401, detail="user email header missing")
    try:
        db.dashboard_delete(x_forwarded_email, config_id)
        db.audit_append(x_forwarded_email, "DASHBOARD_DELETE", config_id, "ok")
    except Exception as e:
        db.audit_append(
            x_forwarded_email, "DASHBOARD_DELETE", config_id, "error",
            {"error": str(e)},
        )
        raise HTTPException(status_code=500, detail=str(e))
    return {"ok": True, "config_id": config_id}
