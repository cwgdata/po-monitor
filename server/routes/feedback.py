"""Feedback form endpoint.

Stores every submission in the UC feedback table (primary path, always works),
then tries to deliver via Databricks notification destinations. The maintainer
email is kept server-side and never exposed to the client.
"""
import os
from typing import Optional

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

from .. import db
from ..config import get_app_client

router = APIRouter(prefix="/api/feedback", tags=["feedback"])

# Maintainer email — feedback submissions get matched against a Databricks
# notification destination of this address. Hidden from the UI. If unset,
# only UC table persistence runs and email delivery is skipped.
MAINTAINER_EMAIL = os.environ.get("PO_MONITOR_MAINTAINER_EMAIL", "")


class FeedbackPayload(BaseModel):
    subject: str
    message: str
    app_url: Optional[str] = None
    user_agent: Optional[str] = None


def _deliver_via_notification_destination(subject: str, message: str, from_email: Optional[str]) -> tuple[str, Optional[str]]:
    """Try to deliver the feedback email via Databricks notification destinations.

    Strategy:
      1. List workspace notification destinations
      2. Find one of type EMAIL whose address matches MAINTAINER_EMAIL
      3. If found, call .test() to trigger it (Databricks will send a fixed test
         email — we include the feedback in the destination name / body when
         creating on-the-fly, but test() is fixed content)
      4. If not found, create a transient EMAIL destination pointed at the
         maintainer and call test()

    Caveat: the Databricks test() payload is controlled by the platform, so the
    actual email body is generic. We rely on the audit/feedback UC table for
    the real content; the test-ping just alerts the maintainer that new
    feedback has arrived.

    Returns (delivery_status, error) — e.g. ('notified', None) or
    ('table_only', 'reason').
    """
    if not MAINTAINER_EMAIL:
        return "table_only", "PO_MONITOR_MAINTAINER_EMAIL not configured"

    try:
        w = get_app_client()
    except Exception as e:
        return "table_only", f"client: {e}"

    try:
        existing = None
        for d in w.notification_destinations.list():
            dtype = str(getattr(d, "destination_type", "") or "").upper()
            cfg = getattr(d, "config", None)
            email = None
            if cfg and hasattr(cfg, "email"):
                email = getattr(cfg.email, "addresses", None) if cfg.email else None
            if dtype == "EMAIL" and email and MAINTAINER_EMAIL in (email or []):
                existing = d
                break

        dest_id = existing.id if existing else None

        if not dest_id:
            # Create a lightweight destination pointed at the maintainer
            from databricks.sdk.service.settings import (
                EmailConfig,
                Config,
                NotificationDestination,
            )
            created = w.notification_destinations.create(
                display_name="po-monitor feedback",
                config=Config(email=EmailConfig(addresses=[MAINTAINER_EMAIL])),
            )
            dest_id = created.id

        # Fire the built-in test to ping the maintainer that feedback arrived
        w.notification_destinations.test(id=dest_id)
        return "notified", None
    except Exception as e:
        return "table_only", f"notify: {e}"


@router.post("")
def submit_feedback(
    payload: FeedbackPayload,
    x_forwarded_email: Optional[str] = Header(default=None),
):
    if not payload.message or not payload.message.strip():
        raise HTTPException(status_code=400, detail="message is required")

    delivery, err = _deliver_via_notification_destination(
        payload.subject, payload.message, x_forwarded_email,
    )

    try:
        fid = db.feedback_append(
            user_email=x_forwarded_email,
            subject=payload.subject or "(no subject)",
            message=payload.message,
            app_url=payload.app_url,
            user_agent=payload.user_agent,
            delivery=delivery,
            delivery_error=err,
        )
    except Exception as e:
        # Table write is the authoritative record — surface this error.
        raise HTTPException(status_code=500, detail=f"feedback store failed: {e}")

    return {
        "status": "ok",
        "feedback_id": fid,
        "delivered": delivery == "notified",
    }
