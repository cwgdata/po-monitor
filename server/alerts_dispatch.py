"""Alert delivery — Slack webhooks + Databricks email destinations.

Mirrors the contract used by feedback.py for email delivery (notification
destinations + .test() ping). Slack uses a plain webhook POST.

Returns a {slack: status, email: status} dict so the caller can record it
verbatim into alert_events.delivery as e.g. "slack:ok,email:ok" or
"slack:fail:HTTP 500,email:skipped".
"""
from __future__ import annotations

from typing import Optional

import httpx

from . import db
from .config import get_app_client


def _slack_payload(event: dict, rule: dict) -> dict:
    """Build a friendly Slack message from a rule + evaluation event."""
    cat = event.get("catalog") or rule.get("catalog") or "*"
    sch = event.get("schema_name") or rule.get("schema_name") or "*"
    tbl = event.get("table_name") or rule.get("table_name") or "*"
    target = f"{cat}.{sch}.{tbl}"
    rt = event.get("rule_type") or rule.get("rule_type") or ""
    obs = event.get("observed_value")
    thr = event.get("threshold") if event.get("threshold") is not None else rule.get("threshold")
    msg = event.get("message") or ""
    text = (
        f":rotating_light: *PO Monitor alert: {rt}*\n"
        f"*Target:* `{target}`\n"
        f"*Threshold:* `{thr}`  *Observed:* `{obs}`\n"
        f"{msg}"
    )
    return {"text": text}


def _slack_send(webhook: str, event: dict, rule: dict, timeout: float = 10.0) -> str:
    """POST to Slack webhook. Returns 'ok' or 'fail:<reason>'."""
    if not webhook:
        return "skipped"
    try:
        r = httpx.post(webhook, json=_slack_payload(event, rule), timeout=timeout)
        if r.status_code >= 400:
            return f"fail:HTTP {r.status_code}"
        return "ok"
    except Exception as e:
        return f"fail:{type(e).__name__}:{str(e)[:120]}"


def _email_send(address: str, event: dict, rule: dict) -> str:
    """Ensure a notification destination exists for `address` and ping it.

    Same pattern as feedback._deliver_via_notification_destination — Databricks
    controls the test() body, so the actual email is generic; the alert_events
    row is the source of truth for the observed value / message.
    """
    if not address:
        return "skipped"
    try:
        w = get_app_client()
    except Exception as e:
        return f"fail:client:{e}"

    try:
        existing_id: Optional[str] = None
        for d in w.notification_destinations.list():
            dtype = str(getattr(d, "destination_type", "") or "").upper()
            cfg = getattr(d, "config", None)
            email = None
            if cfg and hasattr(cfg, "email"):
                email = getattr(cfg.email, "addresses", None) if cfg.email else None
            if dtype == "EMAIL" and email and address in (email or []):
                existing_id = d.id
                break

        if not existing_id:
            from databricks.sdk.service.settings import (
                EmailConfig,
                Config,
            )
            created = w.notification_destinations.create(
                display_name=f"po-monitor alert -> {address}",
                config=Config(email=EmailConfig(addresses=[address])),
            )
            existing_id = created.id

        w.notification_destinations.test(id=existing_id)
        return "ok"
    except Exception as e:
        return f"fail:{type(e).__name__}:{str(e)[:120]}"


def dispatch(rule: dict, event: dict, dry_run: bool = False) -> dict:
    """Deliver an event for a rule to Slack and/or email.

    `rule.slack_webhook` overrides the global config; if blank, fall back to
    the rule-creator's per-user `slack_webhook_url` config (or global).

    Returns {slack, email, summary, error} where summary is a single string
    (e.g. "slack:ok,email:ok") suitable for the alert_events.delivery column.
    """
    # Resolve slack webhook with fallback to user/global config
    webhook = (rule.get("slack_webhook") or "").strip()
    if not webhook:
        try:
            webhook = (db.config_get(
                "slack_webhook_url",
                user_email=rule.get("created_by") or None,
            ) or "").strip()
        except Exception:
            webhook = ""

    email = (rule.get("email") or "").strip()

    if dry_run:
        slack_status = "dry_run" if webhook else "skipped"
        email_status = "dry_run" if email else "skipped"
        summary = f"slack:{slack_status},email:{email_status}"
        return {
            "slack": slack_status,
            "email": email_status,
            "summary": summary,
            "error": None,
            "payload": _slack_payload(event, rule),
            "webhook_set": bool(webhook),
            "email_set": bool(email),
        }

    slack_status = _slack_send(webhook, event, rule) if webhook else "skipped"
    email_status = _email_send(email, event, rule) if email else "skipped"
    summary = f"slack:{slack_status},email:{email_status}"
    err = None
    for v in (slack_status, email_status):
        if v.startswith("fail:"):
            err = v if err is None else f"{err}; {v}"
    return {
        "slack": slack_status,
        "email": email_status,
        "summary": summary,
        "error": err,
    }
