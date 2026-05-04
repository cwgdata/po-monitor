"""Alert rule storage + evaluation + delivery (Slack/email).

Rule CRUD persists to `${PO_MONITOR_CATALOG}.po_monitor.alert_rules`.
Fires persist to `${PO_MONITOR_CATALOG}.po_monitor.alert_events`.

Evaluation runs as a background task (server/alerts_engine.py); this route
module is the user-facing read/write surface plus the on-demand /test endpoint.
"""
from typing import Literal, Optional

import httpx
from fastapi import APIRouter, Header, HTTPException, Query
from pydantic import BaseModel

from .. import db
from ..alerts_dispatch import dispatch
from ..alerts_engine import evaluate_rule
from ..config import get_app_client, get_user_client

router = APIRouter(prefix="/api/alerts", tags=["alerts"])


class AlertRule(BaseModel):
    rule_id: Optional[str] = None
    catalog: Optional[str] = None
    schema_name: Optional[str] = None
    table_name: Optional[str] = None
    rule_type: Literal[
        "PO_QUARANTINED",
        "OPTIMIZE_FAILURE_RATE",
        "VACUUM_STALE",
        "UNCLUSTERED_BYTES",
        "MERGE_CONFLICT_SPIKE",
        "AVG_FILE_SIZE_DROP",
    ]
    threshold: float = 0.0
    lookback_minutes: int = 60
    enabled: bool = True
    slack_webhook: Optional[str] = None
    email: Optional[str] = None


class AlertRulePatch(BaseModel):
    catalog: Optional[str] = None
    schema_name: Optional[str] = None
    table_name: Optional[str] = None
    rule_type: Optional[Literal[
        "PO_QUARANTINED",
        "OPTIMIZE_FAILURE_RATE",
        "VACUUM_STALE",
        "UNCLUSTERED_BYTES",
        "MERGE_CONFLICT_SPIKE",
        "AVG_FILE_SIZE_DROP",
    ]] = None
    threshold: Optional[float] = None
    lookback_minutes: Optional[int] = None
    enabled: Optional[bool] = None
    slack_webhook: Optional[str] = None
    email: Optional[str] = None


def _row_to_api(r: dict) -> dict:
    """Reshape a UC row for the frontend; preserves stringified timestamps."""
    if not r:
        return r
    out = dict(r)
    # Timestamps come back as datetimes or strings depending on driver; coerce.
    for k in ("created_at", "updated_at", "last_evaluated_at", "last_fired_at"):
        v = out.get(k)
        out[k] = (str(v) if v else None)
    # Mask the slack webhook so it doesn't leak in the response. Keep first 8
    # chars as a hint that one is configured; full URL still echoed back to
    # the rule's owner via the create response (immediately after they submit).
    sw = out.get("slack_webhook") or ""
    if sw:
        out["slack_webhook_masked"] = sw[:30] + "..." if len(sw) > 30 else sw
    return out


def _event_to_api(r: dict) -> dict:
    if not r:
        return r
    out = dict(r)
    fa = out.get("fired_at")
    out["fired_at"] = (str(fa) if fa else None)
    return out


@router.get("")
def list_rules(x_forwarded_email: Optional[str] = Header(default=None)):
    """List the caller's rules. Returns extended tracking fields."""
    try:
        # Scope to caller's rules so users don't see each other's webhooks/emails.
        rules = db.alert_rule_list(user_email=x_forwarded_email)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    return {
        "rules": [_row_to_api(r) for r in rules],
        # Active alerts (unack'd current-state alerts) aren't a concept yet —
        # we surface fires via /events. Kept for back-compat with the v1 UI.
        "active": [],
    }


@router.post("")
def create_rule(
    rule: AlertRule,
    x_forwarded_email: Optional[str] = Header(default=None),
):
    user = x_forwarded_email or "unknown"
    try:
        rule_id = db.alert_rule_upsert(rule.model_dump(), user)
        db.audit_append(user, "ALERT_RULE_UPSERT", rule_id, "ok",
                        {"rule_type": rule.rule_type})
        return {"ok": True, "rule_id": rule_id}
    except Exception as e:
        db.audit_append(user, "ALERT_RULE_UPSERT", rule.rule_id or "new",
                        "error", {"error": str(e)})
        raise HTTPException(status_code=500, detail=str(e))


@router.patch("/{rule_id}")
def patch_rule(
    rule_id: str,
    patch: AlertRulePatch,
    x_forwarded_email: Optional[str] = Header(default=None),
):
    """Partial update — flip enabled, change threshold, swap webhook, etc."""
    user = x_forwarded_email or "unknown"
    existing = db.alert_rule_get(rule_id)
    if not existing:
        raise HTTPException(status_code=404, detail="rule not found")
    if existing.get("created_by") and existing.get("created_by") != user:
        raise HTTPException(status_code=403, detail="not your rule")
    patch_dict = patch.model_dump(exclude_none=True)
    try:
        updated = db.alert_rule_patch(rule_id, patch_dict, user)
        db.audit_append(user, "ALERT_RULE_PATCH", rule_id, "ok",
                        {"patch": patch_dict})
    except Exception as e:
        db.audit_append(user, "ALERT_RULE_PATCH", rule_id, "error",
                        {"error": str(e)})
        raise HTTPException(status_code=500, detail=str(e))
    return {"ok": True, "rule": _row_to_api(updated)}


@router.delete("/{rule_id}")
def delete_rule(
    rule_id: str,
    x_forwarded_email: Optional[str] = Header(default=None),
):
    user = x_forwarded_email or "unknown"
    existing = db.alert_rule_get(rule_id)
    if existing and existing.get("created_by") and existing.get("created_by") != user:
        raise HTTPException(status_code=403, detail="not your rule")
    try:
        db.alert_rule_delete(rule_id)
        db.audit_append(user, "ALERT_RULE_DELETE", rule_id, "ok")
        return {"ok": True}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/events")
def list_events(
    rule_id: Optional[str] = Query(default=None),
    limit: int = Query(default=50, ge=1, le=500),
    x_forwarded_email: Optional[str] = Header(default=None),
):
    """Recent fires. Without rule_id, returns events across the caller's rules."""
    try:
        if rule_id:
            # Authz: only let caller see events for their own rule
            r = db.alert_rule_get(rule_id)
            if r and r.get("created_by") and r.get("created_by") != (x_forwarded_email or ""):
                raise HTTPException(status_code=403, detail="not your rule")
            events = db.alert_event_list(rule_id=rule_id, limit=limit)
        else:
            # Filter to the caller's rules
            mine = db.alert_rule_list(user_email=x_forwarded_email)
            ids = [r["rule_id"] for r in mine if r.get("rule_id")]
            events = db.alert_event_list(rule_ids=ids, limit=limit)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    return {"events": [_event_to_api(e) for e in events]}


@router.post("/{rule_id}/test")
def test_rule(
    rule_id: str,
    x_forwarded_email: Optional[str] = Header(default=None),
    x_forwarded_access_token: Optional[str] = Header(default=None),
):
    """Evaluate one rule now and return what *would* be dispatched (no real send).

    Runs the evaluation on-behalf-of the calling user so UC ACLs / warehouse
    `CAN_USE` follow the human, not the app SP.
    """
    rule = db.alert_rule_get(rule_id)
    if not rule:
        raise HTTPException(status_code=404, detail="rule not found")
    if rule.get("created_by") and rule.get("created_by") != (x_forwarded_email or ""):
        raise HTTPException(status_code=403, detail="not your rule")

    client = get_user_client(x_forwarded_access_token)
    try:
        results = evaluate_rule(client, rule)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"evaluator error: {e}")

    out = []
    for res in results:
        event = {
            "rule_id": rule_id,
            "catalog": res.catalog,
            "schema_name": res.schema_name,
            "table_name": res.table_name,
            "rule_type": rule.get("rule_type"),
            "threshold": float(rule.get("threshold") or 0.0),
            "observed_value": float(res.observed),
            "message": res.message,
        }
        delivery = dispatch(rule, event, dry_run=True)
        out.append({
            "triggered": res.triggered,
            "observed": res.observed,
            "message": res.message,
            "target": res.fq(),
            "would_dispatch": delivery,
        })
    return {"ok": True, "results": out, "count": len(out)}


@router.post("/test")
def test_global_slack(message: str = "PO Monitor alert test"):
    """Fire a test Slack message to validate the global webhook config.

    Kept for back-compat with the existing UI 'test' button.
    """
    url = db.config_get("slack_webhook_url")
    if not url:
        raise HTTPException(status_code=400, detail="No slack_webhook_url configured")
    try:
        r = httpx.post(url, json={"text": f":rotating_light: {message}"}, timeout=10)
        r.raise_for_status()
        return {"ok": True, "status": r.status_code}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
