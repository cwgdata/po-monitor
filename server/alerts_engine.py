"""Alert rule evaluation + tick loop.

Runs as a background task (started in app.py lifespan). Every TICK_SECONDS:

  - Read all enabled rules from `${PO_MONITOR_CATALOG}.po_monitor.alert_rules`
  - For each rule:
      - Resolve target table set (wildcard rules expand via UC info_schema)
      - Evaluate each (rule, table) pair via the appropriate evaluator
      - If triggered AND not throttled -> dispatch + record event
      - Update last_evaluated_at / last_status on the rule

Throttling:
  - For a *non-wildcard* rule, we use last_fired_at on the rule itself; the
    rule won't re-fire more than once per max(60min, lookback_minutes).
  - For *wildcard* rules, throttling is per-(rule, fully-qualified-table)
    via a recent alert_events lookup. Same window.

All SQL runs on the app SP — there's no user context in this loop. The SP
needs SELECT on system.* and the target tables; if it doesn't, evaluators
catch the exception and report ERROR (no alert spam).

Each evaluator is intentionally small — the heavy queries live in
server/routes/po.py for the live UI, but those are user-scoped and OBO-authed.
For the worker we run leaner SQL directly.
"""
from __future__ import annotations

import asyncio
import json
import traceback
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from . import db
from .alerts_dispatch import dispatch
from .config import get_app_client
from .sql_client import execute_sql, rows_as_dicts


TICK_SECONDS = 60        # how often the loop wakes
PER_RULE_BUDGET = 30.0   # seconds — bail on a rule taking longer
MIN_THROTTLE_MIN = 60    # minimum throttle window in minutes


# ----------------------------------------------------------------------------
# Result shape
# ----------------------------------------------------------------------------

@dataclass
class EvaluationResult:
    triggered: bool
    observed: float
    message: str
    catalog: str
    schema_name: str
    table_name: str

    def fq(self) -> str:
        return f"{self.catalog}.{self.schema_name}.{self.table_name}"


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _parse_ts(v: Any) -> Optional[datetime]:
    if not v:
        return None
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
    s = str(v).strip()
    if not s or s.lower() == "none":
        return None
    s2 = s.replace(" ", "T")
    if s2.endswith("Z"):
        s2 = s2[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s2)
    except Exception:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _as_int(v) -> Optional[int]:
    if v is None or v == "":
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


# ----------------------------------------------------------------------------
# Target expansion (wildcard rules)
# ----------------------------------------------------------------------------

def _expand_targets(client, rule: dict) -> list[tuple[str, str, str]]:
    """Return the list of (catalog, schema, table) tuples this rule applies to.

    If all three are set on the rule, return just that triple. If any are
    blank, expand via system.information_schema.tables — the SP needs USE
    CATALOG / USE SCHEMA on the relevant levels for this to work.
    """
    cat = (rule.get("catalog") or "").strip()
    sch = (rule.get("schema_name") or "").strip()
    tbl = (rule.get("table_name") or "").strip()
    if cat and sch and tbl:
        return [(cat, sch, tbl)]

    # Wildcard expansion. Cap at 200 tables per rule per tick to bound cost.
    where = ["table_type IN ('MANAGED', 'EXTERNAL')"]
    params: list[dict] = []
    if cat:
        where.append("table_catalog = :c")
        params.append({"name": "c", "value": cat, "type": "STRING"})
    if sch:
        where.append("table_schema = :s")
        params.append({"name": "s", "value": sch, "type": "STRING"})
    if tbl:
        where.append("table_name = :t")
        params.append({"name": "t", "value": tbl, "type": "STRING"})
    if not where:
        return []
    sql = f"""
        SELECT table_catalog, table_schema, table_name
        FROM system.information_schema.tables
        WHERE {' AND '.join(where)}
        LIMIT 200
    """
    try:
        res = execute_sql(client, sql, parameters=params, wait_timeout="15s")
    except Exception as e:
        print(f"[alerts] target expansion failed for rule={rule.get('rule_id')}: {e}")
        return []
    out: list[tuple[str, str, str]] = []
    for row in res.get("rows", []):
        if not row or len(row) < 3:
            continue
        out.append((row[0], row[1], row[2]))
    return out


# ----------------------------------------------------------------------------
# Per-rule-type evaluators
#
# Every evaluator returns an EvaluationResult or raises. They take the SP
# client + concrete (cat, sch, tbl) — wildcard expansion is upstream.
# ----------------------------------------------------------------------------

def _eval_optimize_failure_rate(client, rule, cat, sch, tbl) -> EvaluationResult:
    """Failure rate of OPTIMIZE/COMPACTION/CLUSTERING runs in the lookback window.

    Threshold is a fraction (0-1).
    """
    lb = max(int(rule.get("lookback_minutes") or 60), 60)
    threshold = float(rule.get("threshold") or 0.0)
    sql = """
        SELECT operation_status, count(*) AS n
        FROM system.storage.predictive_optimization_operations_history
        WHERE catalog_name = :c AND schema_name = :s AND table_name = :t
          AND operation_type IN ('OPTIMIZE', 'COMPACTION', 'CLUSTERING')
          AND start_time >= current_timestamp() - make_interval(0, 0, 0, 0, 0, :lb, 0)
        GROUP BY operation_status
    """
    res = execute_sql(client, sql, parameters=[
        {"name": "c", "value": cat, "type": "STRING"},
        {"name": "s", "value": sch, "type": "STRING"},
        {"name": "t", "value": tbl, "type": "STRING"},
        {"name": "lb", "value": str(lb), "type": "INT"},
    ], wait_timeout="20s")
    by_status = {row[0]: row[1] for row in res.get("rows", []) if row and row[0]}
    total = sum(by_status.values())
    failed = (by_status.get("FAILED", 0) or 0)
    rate = (failed / total) if total else 0.0
    return EvaluationResult(
        triggered=(total > 0 and rate > threshold),
        observed=round(rate, 4),
        message=(
            f"OPTIMIZE family failure rate {rate:.0%} ({failed}/{total}) "
            f"in last {lb}m exceeds threshold {threshold:.0%}"
            if total else f"No OPTIMIZE runs in last {lb}m"
        ),
        catalog=cat, schema_name=sch, table_name=tbl,
    )


def _eval_vacuum_stale(client, rule, cat, sch, tbl) -> EvaluationResult:
    """No successful VACUUM in the last `threshold` days. threshold is days."""
    # Use `is None` not `or` — 0.0 is a legitimate threshold (alert on any staleness)
    raw = rule.get("threshold")
    days = float(raw) if raw is not None else 14.0
    sql = """
        SELECT max(end_time) AS last_vac
        FROM system.storage.predictive_optimization_operations_history
        WHERE catalog_name = :c AND schema_name = :s AND table_name = :t
          AND operation_type = 'VACUUM'
          AND operation_status = 'SUCCESSFUL'
    """
    res = execute_sql(client, sql, parameters=[
        {"name": "c", "value": cat, "type": "STRING"},
        {"name": "s", "value": sch, "type": "STRING"},
        {"name": "t", "value": tbl, "type": "STRING"},
    ], wait_timeout="20s")
    last_vac = None
    rows = res.get("rows", [])
    if rows and rows[0] and rows[0][0]:
        last_vac = _parse_ts(rows[0][0])

    # Also consider manual VACUUMs in DESCRIBE HISTORY — the SP-driven PO
    # table won't see user-run VACUUMs.
    try:
        hist = execute_sql(
            client,
            f"DESCRIBE HISTORY `{cat}`.`{sch}`.`{tbl}` LIMIT 200",
            wait_timeout="15s",
        )
        for row in rows_as_dicts(hist):
            op = (row.get("operation") or "").upper()
            if op != "VACUUM END":
                continue
            ts = _parse_ts(row.get("timestamp"))
            if ts and (last_vac is None or ts > last_vac):
                last_vac = ts
    except Exception:
        pass  # absence of history isn't fatal — system table is authoritative-ish

    now = _now_utc()
    if last_vac is None:
        age_days = 9999.0
        msg = f"VACUUM has never succeeded for this table"
    else:
        age_days = (now - last_vac).total_seconds() / 86400.0
        msg = f"Last successful VACUUM was {age_days:.1f}d ago (threshold {days}d)"
    return EvaluationResult(
        triggered=(age_days > days),
        observed=round(age_days, 2),
        message=msg,
        catalog=cat, schema_name=sch, table_name=tbl,
    )


def _eval_unclustered_bytes(client, rule, cat, sch, tbl) -> EvaluationResult:
    """Bytes written since last successful OPTIMIZE / total bytes >= threshold."""
    raw = rule.get("threshold")
    threshold = float(raw) if raw is not None else 0.20
    fq = f"`{cat}`.`{sch}`.`{tbl}`"
    # Find last successful OPTIMIZE/COMPACTION/CLUSTERING in DESCRIBE HISTORY
    hist = execute_sql(
        client, f"DESCRIBE HISTORY {fq} LIMIT 200", wait_timeout="20s",
    )
    rows = rows_as_dicts(hist)
    pivot_ts: Optional[str] = None
    for r in rows:
        op = (r.get("operation") or "").upper()
        if op == "OPTIMIZE":
            pivot_ts = str(r.get("timestamp") or "")
            break
    bytes_since = 0
    if pivot_ts:
        for r in rows:
            ts = str(r.get("timestamp") or "")
            if ts <= pivot_ts:
                continue
            op = (r.get("operation") or "").upper()
            if op not in ("WRITE", "MERGE", "STREAMING UPDATE", "UPDATE", "APPEND", "INSERT"):
                continue
            raw = r.get("operationMetrics") or "{}"
            try:
                m = json.loads(raw) if isinstance(raw, str) else (raw or {})
            except (ValueError, TypeError):
                m = {}
            bytes_since += _as_int(m.get("numAddedBytes")) or 0
    # Total table bytes from DESC DETAIL
    total = 0
    try:
        dd = execute_sql(client, f"DESCRIBE DETAIL {fq}", wait_timeout="15s")
        dd_rows = rows_as_dicts(dd)
        if dd_rows:
            total = _as_int(dd_rows[0].get("sizeInBytes")) or 0
    except Exception:
        pass
    frac = (bytes_since / total) if total else 0.0
    return EvaluationResult(
        triggered=(total > 0 and frac >= threshold),
        observed=round(frac, 4),
        message=(
            f"Unclustered bytes {frac:.0%} ({bytes_since:,}/{total:,}) "
            f">= threshold {threshold:.0%}"
            if pivot_ts else f"No prior OPTIMIZE found; can't compute unclustered fraction"
        ),
        catalog=cat, schema_name=sch, table_name=tbl,
    )


def _eval_avg_file_size_drop(client, rule, cat, sch, tbl) -> EvaluationResult:
    """Avg file size now vs 7d ago dropped > threshold (fraction)."""
    raw = rule.get("threshold")
    threshold = float(raw) if raw is not None else 0.15
    sql = """
        SELECT active_files, active_bytes
        FROM system.storage.table_metrics_history
        WHERE catalog_name = :c AND schema_name = :s AND table_name = :t
          AND snapshot_date = current_date() - INTERVAL 7 DAY
        LIMIT 1
    """
    res = execute_sql(client, sql, parameters=[
        {"name": "c", "value": cat, "type": "STRING"},
        {"name": "s", "value": sch, "type": "STRING"},
        {"name": "t", "value": tbl, "type": "STRING"},
    ], wait_timeout="20s")
    rows = res.get("rows", [])
    if not rows or not rows[0]:
        return EvaluationResult(
            triggered=False, observed=0.0,
            message="No 7d-ago snapshot in system.storage.table_metrics_history",
            catalog=cat, schema_name=sch, table_name=tbl,
        )
    prev_files = _as_int(rows[0][0]) or 0
    prev_bytes = _as_int(rows[0][1]) or 0
    if not prev_files or not prev_bytes:
        return EvaluationResult(
            triggered=False, observed=0.0,
            message="7d-ago snapshot is empty",
            catalog=cat, schema_name=sch, table_name=tbl,
        )
    prev_avg = prev_bytes / prev_files
    cur_files = 0
    cur_bytes = 0
    try:
        dd = execute_sql(
            client, f"DESCRIBE DETAIL `{cat}`.`{sch}`.`{tbl}`", wait_timeout="15s",
        )
        dd_rows = rows_as_dicts(dd)
        if dd_rows:
            cur_files = _as_int(dd_rows[0].get("numFiles")) or 0
            cur_bytes = _as_int(dd_rows[0].get("sizeInBytes")) or 0
    except Exception as e:
        return EvaluationResult(
            triggered=False, observed=0.0,
            message=f"DESCRIBE DETAIL failed: {e}",
            catalog=cat, schema_name=sch, table_name=tbl,
        )
    if not cur_files:
        return EvaluationResult(
            triggered=False, observed=0.0,
            message="Current numFiles is zero",
            catalog=cat, schema_name=sch, table_name=tbl,
        )
    cur_avg = cur_bytes / cur_files
    drop = (prev_avg - cur_avg) / prev_avg if prev_avg else 0.0
    return EvaluationResult(
        triggered=(drop > threshold),
        observed=round(drop, 4),
        message=(
            f"Avg file size dropped {drop:.0%} WoW "
            f"({int(prev_avg):,} -> {int(cur_avg):,} bytes); threshold {threshold:.0%}"
        ),
        catalog=cat, schema_name=sch, table_name=tbl,
    )


def _eval_merge_conflict_spike(client, rule, cat, sch, tbl) -> EvaluationResult:
    """Count of MERGE conflicts in the lookback window > threshold."""
    raw = rule.get("threshold")
    threshold = float(raw) if raw is not None else 5.0
    lb = max(int(rule.get("lookback_minutes") or 60), 15)
    fq = f"{cat}.{sch}.{tbl}"
    sql = """
        SELECT count(*) AS n
        FROM system.query.history
        WHERE statement_type = 'MERGE'
          AND start_time >= current_timestamp() - make_interval(0, 0, 0, 0, 0, :lb, 0)
          AND execution_status <> 'FINISHED'
          AND error_message ILIKE '%DELTA_CONCURRENT%'
          AND (
            statement_text ILIKE CONCAT('%', :fq, '%')
            OR statement_text ILIKE CONCAT('%`', :cat, '`.`', :sch, '`.`', :tbl, '`%')
          )
    """
    res = execute_sql(client, sql, parameters=[
        {"name": "lb", "value": str(lb), "type": "INT"},
        {"name": "fq", "value": fq, "type": "STRING"},
        {"name": "cat", "value": cat, "type": "STRING"},
        {"name": "sch", "value": sch, "type": "STRING"},
        {"name": "tbl", "value": tbl, "type": "STRING"},
    ], wait_timeout="20s")
    rows = res.get("rows", [])
    n = _as_int(rows[0][0]) if rows and rows[0] else 0
    n = n or 0
    return EvaluationResult(
        triggered=(n > threshold),
        observed=float(n),
        message=f"{n} MERGE conflicts in last {lb}m (threshold {int(threshold)})",
        catalog=cat, schema_name=sch, table_name=tbl,
    )


def _eval_po_quarantined(client, rule, cat, sch, tbl) -> EvaluationResult:
    """PO is in quarantine for this table.

    Tries the explicit `is_in_quarantine` column first (if present on the
    workspace's PO history table); otherwise infers from "3+ consecutive
    failures and no successful run in the last 3 days".
    """
    # Strategy 1: try the canonical column first.
    try:
        sql = """
            SELECT max(case when is_in_quarantine then 1 else 0 end) AS quarantined
            FROM system.storage.predictive_optimization_operations_history
            WHERE catalog_name = :c AND schema_name = :s AND table_name = :t
              AND start_time >= current_timestamp() - INTERVAL 14 DAY
        """
        res = execute_sql(client, sql, parameters=[
            {"name": "c", "value": cat, "type": "STRING"},
            {"name": "s", "value": sch, "type": "STRING"},
            {"name": "t", "value": tbl, "type": "STRING"},
        ], wait_timeout="20s")
        rows = res.get("rows", [])
        if rows and rows[0] and rows[0][0] is not None:
            q = bool(_as_int(rows[0][0]))
            return EvaluationResult(
                triggered=q,
                observed=1.0 if q else 0.0,
                message=("PO is quarantined for this table" if q
                         else "PO is not in quarantine"),
                catalog=cat, schema_name=sch, table_name=tbl,
            )
    except Exception:
        pass  # column not present on this workspace; fall through to heuristic

    # Strategy 2: inferred quarantine — last 5 PO runs (any optimize-family op),
    # check if 3+ consecutive most-recent failures and no run in last 3 days.
    sql = """
        SELECT operation_status, end_time
        FROM system.storage.predictive_optimization_operations_history
        WHERE catalog_name = :c AND schema_name = :s AND table_name = :t
          AND operation_type IN ('OPTIMIZE','COMPACTION','CLUSTERING')
        ORDER BY start_time DESC
        LIMIT 5
    """
    try:
        res = execute_sql(client, sql, parameters=[
            {"name": "c", "value": cat, "type": "STRING"},
            {"name": "s", "value": sch, "type": "STRING"},
            {"name": "t", "value": tbl, "type": "STRING"},
        ], wait_timeout="20s")
    except Exception as e:
        return EvaluationResult(
            triggered=False, observed=0.0,
            message=f"PO history query failed: {e}",
            catalog=cat, schema_name=sch, table_name=tbl,
        )
    runs = res.get("rows", [])
    if not runs:
        return EvaluationResult(
            triggered=False, observed=0.0,
            message="No PO runs in history; can't infer quarantine",
            catalog=cat, schema_name=sch, table_name=tbl,
        )
    # Count consecutive failures from newest
    consec = 0
    for r in runs:
        if not r:
            continue
        if (r[0] or "").upper() == "FAILED":
            consec += 1
        else:
            break
    last_run_ts = _parse_ts(runs[0][1] if runs[0] and len(runs[0]) > 1 else None)
    now = _now_utc()
    age_days = (now - last_run_ts).total_seconds() / 86400.0 if last_run_ts else 9999.0
    quarantined = consec >= 3 and age_days >= 3.0
    return EvaluationResult(
        triggered=quarantined,
        observed=float(consec),
        message=(
            f"PO appears quarantined: {consec} consecutive failures, "
            f"last run {age_days:.1f}d ago"
            if quarantined else
            f"PO not quarantined ({consec} consec failures, "
            f"last run {age_days:.1f}d ago)"
        ),
        catalog=cat, schema_name=sch, table_name=tbl,
    )


_EVALUATORS = {
    "PO_QUARANTINED": _eval_po_quarantined,
    "OPTIMIZE_FAILURE_RATE": _eval_optimize_failure_rate,
    "VACUUM_STALE": _eval_vacuum_stale,
    "UNCLUSTERED_BYTES": _eval_unclustered_bytes,
    "AVG_FILE_SIZE_DROP": _eval_avg_file_size_drop,
    "MERGE_CONFLICT_SPIKE": _eval_merge_conflict_spike,
}


def evaluate_rule_for_table(client, rule: dict, cat: str, sch: str, tbl: str) -> EvaluationResult:
    """Dispatch one (rule, table) evaluation. Raises on unknown rule_type."""
    rt = rule.get("rule_type") or ""
    fn = _EVALUATORS.get(rt)
    if not fn:
        raise ValueError(f"unknown rule_type: {rt}")
    return fn(client, rule, cat, sch, tbl)


def evaluate_rule(client, rule: dict) -> list[EvaluationResult]:
    """Evaluate a rule across all targets it matches. Returns one result per table."""
    targets = _expand_targets(client, rule)
    if not targets:
        return []
    out: list[EvaluationResult] = []
    for (cat, sch, tbl) in targets:
        try:
            out.append(evaluate_rule_for_table(client, rule, cat, sch, tbl))
        except Exception as e:
            # Surface as a non-triggering ERROR result so the loop records it
            # and the UI can show "last_status=ERROR" instead of silently dying.
            out.append(EvaluationResult(
                triggered=False, observed=0.0,
                message=f"evaluator error: {e}",
                catalog=cat, schema_name=sch, table_name=tbl,
            ))
    return out


# ----------------------------------------------------------------------------
# Throttling
# ----------------------------------------------------------------------------

def _throttle_window_minutes(rule: dict) -> int:
    """Same condition shouldn't re-fire more than once per max(60, lookback)."""
    return max(MIN_THROTTLE_MIN, int(rule.get("lookback_minutes") or 0))


def _is_throttled(rule: dict, result: EvaluationResult, is_wildcard: bool) -> bool:
    win = _throttle_window_minutes(rule)
    cutoff = _now_utc() - timedelta(minutes=win)
    if is_wildcard:
        # Per-table dedupe: look at recent events for this rule + this fq table
        try:
            recent = db.alert_event_recent_for_rule_table(
                rule_id=rule["rule_id"], fq_table=result.fq(), since=cutoff,
            )
            return recent is not None
        except Exception as e:
            print(f"[alerts] throttle lookup failed: {e}")
            return False
    # Single-target rule: use the rule's own last_fired_at
    last_fired = _parse_ts(rule.get("last_fired_at"))
    if last_fired is None:
        return False
    return last_fired >= cutoff


# ----------------------------------------------------------------------------
# Tick loop
# ----------------------------------------------------------------------------

async def _evaluate_one_rule(client, rule: dict) -> dict:
    """Evaluate a single rule + dispatch any triggered events.

    Returns a small summary dict for logging/auditing. Updates last_evaluated_at
    on the rule before returning regardless of outcome.
    """
    rule_id = rule["rule_id"]
    is_wildcard = not (
        rule.get("catalog") and rule.get("schema_name") and rule.get("table_name")
    )
    summary = {
        "rule_id": rule_id, "evaluated": 0, "fired": 0, "errors": 0, "throttled": 0,
    }
    try:
        results = evaluate_rule(client, rule)
    except Exception as e:
        db.alert_rule_update_run_state(rule_id, {
            "last_evaluated_at": _now_utc(),
            "last_status": "ERROR",
            "last_error": str(e)[:500],
        })
        summary["errors"] += 1
        return summary

    summary["evaluated"] = len(results)
    any_fire = False
    last_err: Optional[str] = None
    for res in results:
        if "evaluator error" in (res.message or ""):
            summary["errors"] += 1
            last_err = res.message
            continue
        if not res.triggered:
            continue
        if _is_throttled(rule, res, is_wildcard):
            summary["throttled"] += 1
            continue

        # Fire: build event, dispatch, persist.
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
        delivery = dispatch(rule, event)
        event["delivery"] = delivery.get("summary")
        event["delivery_error"] = delivery.get("error")
        try:
            db.alert_event_append(event)
        except Exception as e:
            print(f"[alerts] event_append failed for {rule_id}/{res.fq()}: {e}")

        # Audit: every fire goes into the audit table for observability.
        try:
            db.audit_append(
                rule.get("created_by") or "alerts_engine",
                "ALERT_FIRE",
                f"{res.catalog}.{res.schema_name}.{res.table_name}",
                "fired",
                {
                    "rule_id": rule_id,
                    "rule_type": rule.get("rule_type"),
                    "observed": res.observed,
                    "threshold": rule.get("threshold"),
                    "delivery": delivery.get("summary"),
                },
            )
        except Exception:
            pass

        any_fire = True
        summary["fired"] += 1

    state_patch: dict[str, Any] = {"last_evaluated_at": _now_utc()}
    if any_fire:
        state_patch["last_fired_at"] = _now_utc()
        state_patch["last_status"] = "FIRED"
        state_patch["last_error"] = None
    elif last_err:
        state_patch["last_status"] = "ERROR"
        state_patch["last_error"] = last_err[:500]
    else:
        state_patch["last_status"] = "OK"
        state_patch["last_error"] = None
    db.alert_rule_update_run_state(rule_id, state_patch)
    return summary


async def _tick_once():
    try:
        rules = db.alert_rule_list()
    except Exception as e:
        print(f"[alerts] rule_list failed: {e}")
        return
    if not rules:
        return
    client = get_app_client()
    for rule in rules:
        if not rule.get("enabled"):
            continue
        try:
            await asyncio.wait_for(
                _evaluate_one_rule(client, rule),
                timeout=PER_RULE_BUDGET,
            )
        except asyncio.TimeoutError:
            print(f"[alerts] rule {rule.get('rule_id')} exceeded {PER_RULE_BUDGET}s budget")
            try:
                db.alert_rule_update_run_state(rule.get("rule_id"), {
                    "last_evaluated_at": _now_utc(),
                    "last_status": "ERROR",
                    "last_error": f"exceeded {PER_RULE_BUDGET}s budget",
                })
            except Exception:
                pass
        except Exception as e:
            tb = traceback.format_exc()
            print(f"[alerts] rule {rule.get('rule_id')} failed: {e}\n{tb}")


async def alerts_tick_loop():
    """Outer loop. Sleeps TICK_SECONDS between ticks. Cancellation-safe."""
    print(f"[alerts] tick loop started (every {TICK_SECONDS}s)")
    try:
        while True:
            try:
                await _tick_once()
            except Exception as e:
                print(f"[alerts] tick error: {e}")
            await asyncio.sleep(TICK_SECONDS)
    except asyncio.CancelledError:
        print("[alerts] tick loop cancelled")
        raise
