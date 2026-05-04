"""Background worker that fires schedules.

Two schedule types are supported:

- cron:  fires when croniter(cron_expr, last_run_at).get_next() <= now.
         Timezone defaults to UTC; other zones use zoneinfo.

- trigger: polls DESCRIBE HISTORY on the target every poll_interval_seconds.
           If the newest WRITE/MERGE/UPDATE/STREAMING UPDATE/DELETE commit is
           newer than last_run_at AND now - last_run_at >= min_interval_seconds,
           the op fires. Always updates last_checked_at.

All SQL runs as the app service principal — the request context doesn't
exist in the worker, so OBO isn't available. The SP must have MODIFY on each
target table (surfaced as a warning in the Schedules UI).

The loop is single-process: if the app runs multiple replicas, each will
evaluate the same schedules independently, which at worst means N duplicate
OPTIMIZE / VACUUM submissions per tick. That's tolerable for v1 — cleaner
would be a UC-backed leader lock, deferred to later.
"""
from __future__ import annotations

import asyncio
import contextlib
import traceback
from datetime import datetime, timezone, timedelta
from typing import Any, Optional

from . import db
from .config import get_app_client
from .sql_client import execute_sql


# Commit types we consider "new data written" for trigger-mode schedules.
# UPDATE includes MERGE/UPDATE/DELETE already in Delta terminology; we list
# them explicitly so the match is readable and tolerant of Delta's history
# operation names.
_TRIGGER_OPERATIONS = {
    "WRITE",
    "MERGE",
    "UPDATE",
    "STREAMING UPDATE",
    "DELETE",
    "APPEND",
    "CREATE TABLE AS SELECT",
    "REPLACE TABLE AS SELECT",
    "INSERT",
}

TICK_SECONDS = 30  # how often the loop wakes up
DEFAULT_POLL = 300
DEFAULT_MIN = 1800


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _parse_ts(v: Any) -> Optional[datetime]:
    """Parse a stringy UC timestamp to an aware UTC datetime."""
    if not v:
        return None
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
    s = str(v).strip()
    if not s or s.lower() == "none":
        return None
    # UC returns "2024-01-02 03:04:05[.mmm]" or ISO-ish. Normalize the space.
    s2 = s.replace(" ", "T")
    # Strip trailing Z if present (datetime.fromisoformat in older pythons)
    if s2.endswith("Z"):
        s2 = s2[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s2)
    except Exception:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _build_sql(op: str, fqt: str) -> str:
    if op == "OPTIMIZE":
        return f"OPTIMIZE {fqt}"
    if op == "VACUUM_LITE":
        return f"VACUUM {fqt} LITE"
    if op == "VACUUM_FULL":
        return f"VACUUM {fqt} FULL"
    raise ValueError(f"unsupported operation: {op}")


def _fqt(sched: dict) -> str:
    cat = sched.get("catalog") or ""
    sch = sched.get("schema_name") or sched.get("schema") or ""
    tbl = sched.get("table_name") or sched.get("table") or ""
    return f"`{cat}`.`{sch}`.`{tbl}`"


def _plain_target(sched: dict) -> str:
    return f"{sched.get('catalog')}.{sched.get('schema_name') or sched.get('schema')}.{sched.get('table_name') or sched.get('table')}"


def _next_cron(cron_expr: str, tz_name: str, after: datetime) -> Optional[datetime]:
    """Return the next fire time strictly after `after`, in UTC.

    Requires `croniter` to be installed. We pin it in requirements.txt.
    """
    try:
        from croniter import croniter  # type: ignore
    except Exception as e:
        print(f"[scheduler] croniter missing: {e}")
        return None
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo(tz_name or "UTC")
    except Exception:
        tz = timezone.utc
    try:
        # Evaluate in the schedule's tz for DST-correct next-fire, convert back to UTC
        local_after = after.astimezone(tz)
        it = croniter(cron_expr, local_after)
        nxt_local = it.get_next(datetime)
        if nxt_local.tzinfo is None:
            nxt_local = nxt_local.replace(tzinfo=tz)
        return nxt_local.astimezone(timezone.utc)
    except Exception as e:
        print(f"[scheduler] cron parse fail '{cron_expr}': {e}")
        return None


def fire_schedule(sched: dict, manual: bool = False, actor: Optional[str] = None) -> dict:
    """Actually submit the SQL for a schedule and record the result.

    Returns {status, statement_id, state, target, operation}. Logs to audit
    table regardless of outcome. Caller is responsible for ensuring the
    schedule is eligible to fire (cron / trigger logic).
    """
    schedule_id = sched.get("schedule_id")
    op = sched.get("operation") or ""
    target = _plain_target(sched)
    fqt = _fqt(sched)
    wh = sched.get("warehouse_id")
    user = actor or sched.get("user_email") or sched.get("created_by") or "scheduler"
    if not wh:
        msg = "schedule has no warehouse_id"
        db.schedule_update_run_state(schedule_id, {
            "last_run_at": _now_utc(),
            "last_run_status": "error",
            "last_run_error": msg,
        })
        db.audit_append(user, f"SCHEDULE_{op}_FIRE", target, "error",
                        {"schedule_id": schedule_id, "error": msg, "manual": manual})
        raise RuntimeError(msg)

    sql = _build_sql(op, fqt)
    client = get_app_client()
    try:
        res = execute_sql(client, sql, warehouse_id=wh, wait_timeout="5s", fire_and_forget=True)
        stmt_id = res.get("statement_id")
        state = res.get("state")
        db.schedule_update_run_state(schedule_id, {
            "last_run_at": _now_utc(),
            "last_run_status": state or "submitted",
            "last_run_error": None,
            "last_run_statement_id": stmt_id,
        })
        db.audit_append(user, f"SCHEDULE_{op}_FIRE", target, "submitted", {
            "schedule_id": schedule_id,
            "statement_id": stmt_id,
            "state": state,
            "warehouse_id": wh,
            "manual": manual,
        })
        return {
            "status": "submitted",
            "schedule_id": schedule_id,
            "target": target,
            "operation": op,
            "statement_id": stmt_id,
            "state": state,
        }
    except Exception as e:
        tb = traceback.format_exc()
        err_msg = str(e)
        db.schedule_update_run_state(schedule_id, {
            "last_run_at": _now_utc(),
            "last_run_status": "error",
            "last_run_error": err_msg[:500],
        })
        db.audit_append(user, f"SCHEDULE_{op}_FIRE", target, "error", {
            "schedule_id": schedule_id, "error": err_msg, "manual": manual,
        })
        print(f"[scheduler] fire failed for {schedule_id}: {tb.splitlines()[-3:]}")
        raise


def _latest_data_commit(client, fqt: str) -> Optional[datetime]:
    """Return the timestamp of the newest data-modifying commit, or None.

    DESCRIBE HISTORY returns in reverse-chronological order; we scan and
    pick the first row whose operation is in _TRIGGER_OPERATIONS.
    """
    try:
        r = execute_sql(client, f"DESCRIBE HISTORY {fqt} LIMIT 100", wait_timeout="15s")
    except Exception as e:
        print(f"[scheduler] DESCRIBE HISTORY failed for {fqt}: {e}")
        return None
    cols = r.get("columns") or []
    rows = r.get("rows") or []
    try:
        op_idx = cols.index("operation")
        ts_idx = cols.index("timestamp")
    except ValueError:
        return None
    for row in rows:
        if not row or len(row) <= max(op_idx, ts_idx):
            continue
        op = (row[op_idx] or "").upper()
        if op in _TRIGGER_OPERATIONS:
            return _parse_ts(row[ts_idx])
    return None


def _should_fire(sched: dict, now: datetime) -> tuple[bool, str]:
    """Decide whether a schedule should fire right now.

    Returns (fire, reason). Reason is a short string for audit / logs.
    """
    if not sched.get("enabled"):
        return False, "disabled"
    stype = (sched.get("schedule_type") or "cron").lower()
    last_run = _parse_ts(sched.get("last_run_at"))
    last_checked = _parse_ts(sched.get("last_checked_at"))

    if stype == "cron":
        cron_expr = sched.get("cron") or ""
        tz_name = sched.get("timezone") or "UTC"
        if not cron_expr:
            return False, "no cron"
        # Anchor: last_run_at if present, else created_at, else now - 1min
        anchor = last_run or _parse_ts(sched.get("created_at")) or (now - timedelta(minutes=1))
        nxt = _next_cron(cron_expr, tz_name, anchor)
        if nxt is None:
            return False, "cron parse failed"
        return (now >= nxt), f"cron next={nxt.isoformat()}"

    # trigger
    poll = int(sched.get("poll_interval_seconds") or DEFAULT_POLL)
    min_int = int(sched.get("min_interval_seconds") or DEFAULT_MIN)

    # Rate-limit our own HISTORY calls
    if last_checked and (now - last_checked).total_seconds() < poll:
        return False, f"within poll ({poll}s)"
    # No-op if we just fired recently
    if last_run and (now - last_run).total_seconds() < min_int:
        return False, f"min_interval {min_int}s not elapsed"

    # Need to query DESCRIBE HISTORY to decide. Caller handles the actual
    # call because this function is pure decision logic; but we signal
    # "check history" via a special reason string. The tick loop handles it.
    return False, "check history"


async def _tick_once():
    """Evaluate every enabled schedule once."""
    now = _now_utc()
    try:
        schedules = db.schedule_list()
    except Exception as e:
        print(f"[scheduler] schedule_list failed: {e}")
        return
    sp_client = get_app_client()
    for sched in schedules:
        if not sched.get("enabled"):
            continue
        stype = (sched.get("schedule_type") or "cron").lower()
        try:
            fire, reason = _should_fire(sched, now)
        except Exception as e:
            print(f"[scheduler] decision error for {sched.get('schedule_id')}: {e}")
            continue

        if stype == "trigger" and reason == "check history":
            # Query history, then decide
            fqt = _fqt(sched)
            last_commit = _latest_data_commit(sp_client, fqt)
            db.schedule_update_run_state(sched["schedule_id"], {
                "last_checked_at": now,
            })
            if last_commit is None:
                continue
            last_run = _parse_ts(sched.get("last_run_at"))
            min_int = int(sched.get("min_interval_seconds") or DEFAULT_MIN)
            # New commit since last run?
            if last_run and last_commit <= last_run:
                continue
            if last_run and (now - last_run).total_seconds() < min_int:
                continue
            try:
                fire_schedule(sched)
            except Exception:
                pass  # already logged in fire_schedule
            continue

        if fire:
            try:
                fire_schedule(sched)
            except Exception:
                pass
        else:
            # Light breadcrumb for cron to show we're alive.
            # Update last_checked_at so the UI knows the loop is running.
            if stype == "cron":
                db.schedule_update_run_state(sched["schedule_id"], {
                    "last_checked_at": now,
                })


async def schedules_tick_loop():
    """Outer loop. Sleeps TICK_SECONDS between ticks. Cancellation-safe."""
    print(f"[scheduler] tick loop started (every {TICK_SECONDS}s)")
    try:
        while True:
            try:
                await _tick_once()
            except Exception as e:
                print(f"[scheduler] tick error: {e}")
            await asyncio.sleep(TICK_SECONDS)
    except asyncio.CancelledError:
        print("[scheduler] tick loop cancelled")
        raise
