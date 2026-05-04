"""Per-user card cache endpoints.

Lets the frontend save the last-known TableCard payload (health + runs +
trends + merges) to Unity Catalog scoped by user_email, so opening the app
on a new device shows last-known data instantly.
"""
from typing import Any, Optional

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

from .. import db

router = APIRouter(prefix="/api/card-cache", tags=["card-cache"])


class CachePayload(BaseModel):
    catalog: str
    schema: str
    table: str
    payload: dict[str, Any]


@router.get("")
def get_cache(
    catalog: str,
    schema: str,
    table: str,
    x_forwarded_email: Optional[str] = Header(default=None),
):
    if not x_forwarded_email:
        return {"payload": None, "updated_at": None}
    try:
        row = db.card_cache_get(x_forwarded_email, catalog, schema, table)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    if not row:
        return {"payload": None, "updated_at": None}
    return row


@router.post("")
def put_cache(
    body: CachePayload,
    x_forwarded_email: Optional[str] = Header(default=None),
):
    if not x_forwarded_email:
        return {"ok": False, "reason": "no_user"}
    try:
        db.card_cache_save(x_forwarded_email, body.catalog, body.schema, body.table, body.payload)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    return {"ok": True}
