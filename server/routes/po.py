"""PO system-table + DESCRIBE HISTORY queries.

/runs returns a unified view of:
  - PO-driven ops from system.storage.predictive_optimization_operations_history
  - Manual ops (user-triggered OPTIMIZE/VACUUM) from DESCRIBE HISTORY
Each row carries a `source` field ("po" or "manual").
"""
import json
from datetime import datetime, timedelta, timezone
from fastapi import APIRouter, Depends, Header, HTTPException
from typing import Optional

from ..config import get_user_client
from ..sql_client import execute_sql, rows_as_dicts


def _as_int(v):
    if v is None or v == "":
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None

router = APIRouter(prefix="/api/po", tags=["po"])


def _user_client(x_forwarded_access_token: Optional[str] = Header(default=None)):
    return get_user_client(x_forwarded_access_token)


@router.get("/runs")
def get_po_runs(
    catalog: str,
    schema: str,
    table: str,
    lookback_days: int = 30,
    client=Depends(_user_client),
):
    """Return recent PO run history for a table."""
    sql = """
        SELECT
          operation_type,
          operation_status,
          start_time,
          end_time,
          (unix_timestamp(end_time) - unix_timestamp(start_time)) AS duration_seconds,
          try_cast(operation_metrics['number_of_compacted_files']    AS BIGINT) AS files_compacted,
          try_cast(operation_metrics['number_of_output_files']       AS BIGINT) AS files_output,
          try_cast(operation_metrics['amount_of_data_compacted_bytes'] AS BIGINT) AS bytes_compacted,
          try_cast(operation_metrics['amount_of_output_data_bytes']    AS BIGINT) AS bytes_output,
          try_cast(operation_metrics['number_of_deleted_files']      AS BIGINT) AS files_deleted,
          try_cast(operation_metrics['amount_of_data_deleted_bytes'] AS BIGINT) AS bytes_deleted,
          try_cast(operation_metrics['staleness_percentage_reduced'] AS DOUBLE) AS staleness_reduced_pct,
          try_cast(usage_quantity AS DOUBLE) AS dbus,
          operation_metrics
        FROM system.storage.predictive_optimization_operations_history
        WHERE catalog_name = :catalog
          AND schema_name  = :schema
          AND table_name   = :table
          AND start_time >= current_timestamp() - make_interval(0, 0, 0, :lookback_days, 0, 0, 0)
        ORDER BY start_time DESC
        LIMIT 50
    """
    params = [
        {"name": "catalog", "value": catalog, "type": "STRING"},
        {"name": "schema", "value": schema, "type": "STRING"},
        {"name": "table", "value": table, "type": "STRING"},
        {"name": "lookback_days", "value": str(lookback_days), "type": "INT"},
    ]
    # --- 1. PO-driven runs from system table ---
    po_runs: list[dict] = []
    spike = None
    try:
        result = execute_sql(client, sql, parameters=params)
        for r in rows_as_dicts(result):
            r["source"] = "po"
            r["user"] = "predictive_optimization"
            po_runs.append(r)
    except Exception as e:
        msg = str(e)
        if "TABLE_OR_VIEW_NOT_FOUND" in msg or "does not exist" in msg.lower():
            spike = (
                "system.storage.predictive_optimization_operations_history not found in this workspace. "
                "Showing manual ops only."
            )
        else:
            raise HTTPException(status_code=500, detail=msg)

    # --- 2. Manual ops from DESCRIBE HISTORY ---
    manual_runs: list[dict] = []
    cutoff = (datetime.now(timezone.utc) - timedelta(days=lookback_days)).isoformat()
    try:
        hist = execute_sql(client, f"DESCRIBE HISTORY `{catalog}`.`{schema}`.`{table}` LIMIT 200")
        for row in rows_as_dicts(hist):
            op = (row.get("operation") or "").upper()
            # Surface OPTIMIZE, VACUUM END (the completion side of VACUUM), MERGE
            if op == "OPTIMIZE":
                op_type = "OPTIMIZE"
            elif op == "VACUUM END":
                op_type = "VACUUM"
            elif op == "VACUUM START":
                continue  # skip — the END row has the metrics
            elif op == "MERGE":
                op_type = "MERGE"
            else:
                continue

            ts = row.get("timestamp")
            if not ts:
                continue
            # Cheap string compare works on ISO timestamps
            if str(ts) < cutoff:
                continue

            metrics_raw = row.get("operationMetrics") or "{}"
            try:
                m = json.loads(metrics_raw) if isinstance(metrics_raw, str) else (metrics_raw or {})
            except (ValueError, TypeError):
                m = {}

            manual_runs.append({
                "source": "manual",
                "operation_type": op_type,
                "operation_status": "SUCCESSFUL",  # DESCRIBE HISTORY only shows committed ops
                "start_time": ts,
                "end_time": ts,
                "duration_seconds": _as_int(m.get("executionTimeMs")) // 1000 if _as_int(m.get("executionTimeMs")) else None,
                "files_compacted": _as_int(m.get("numRemovedFiles")),
                "files_output": _as_int(m.get("numAddedFiles")),
                "bytes_compacted": _as_int(m.get("numRemovedBytes")),
                "bytes_output": _as_int(m.get("numAddedBytes")),
                "files_deleted": _as_int(m.get("numVacuumedFiles")) or _as_int(m.get("numDeletedFiles")),
                "user": row.get("userName"),
            })
    except Exception as e:
        # DESCRIBE HISTORY can fail for tables the caller can't read fully.
        # Don't let it kill the whole response.
        pass

    # Merge and sort newest-first
    merged = sorted(po_runs + manual_runs, key=lambda r: r.get("start_time") or "", reverse=True)

    # Group OPTIMIZE/COMPACTION/CLUSTERING batches that occurred close together
    # (within 10 minutes, same source + op_type). Typical "OPTIMIZE table" commands
    # emit multiple batches as separate commit rows; collapse them for display.
    OPTIMIZE_OPS = {"OPTIMIZE", "COMPACTION", "CLUSTERING"}
    GROUP_WINDOW_SEC = 600

    def _parse_ts(s: str) -> float:
        if not s:
            return 0.0
        try:
            # Normalize various ISO shapes to a comparable epoch-ish number
            import datetime as _dt
            v = s.replace("Z", "+00:00") if s.endswith("Z") else s
            return _dt.datetime.fromisoformat(v[:26]).timestamp()
        except Exception:
            return 0.0

    grouped: list[dict] = []
    i = 0
    while i < len(merged):
        r = merged[i]
        if (r.get("operation_type") or "").upper() not in OPTIMIZE_OPS:
            r["batch_count"] = 1
            grouped.append(r)
            i += 1
            continue
        # Collect consecutive rows with same source+op_type within the window
        bucket = [r]
        src = r.get("source")
        op = r.get("operation_type")
        anchor_ts = _parse_ts(r.get("start_time") or "")
        j = i + 1
        while j < len(merged):
            nxt = merged[j]
            if (
                nxt.get("source") == src
                and nxt.get("operation_type") == op
                and abs(_parse_ts(nxt.get("start_time") or "") - anchor_ts) <= GROUP_WINDOW_SEC
            ):
                bucket.append(nxt)
                anchor_ts = _parse_ts(nxt.get("start_time") or "")
                j += 1
            else:
                break
        if len(bucket) == 1:
            r["batch_count"] = 1
            grouped.append(r)
        else:
            # Aggregate: sum metrics, keep the earliest start and latest end
            def _sum(key):
                vals = [b.get(key) for b in bucket if b.get(key) is not None]
                return sum(vals) if vals else None
            start_times = [b.get("start_time") for b in bucket if b.get("start_time")]
            end_times = [b.get("end_time") for b in bucket if b.get("end_time")]
            agg = {
                **bucket[0],
                "start_time": min(start_times) if start_times else bucket[0].get("start_time"),
                "end_time": max(end_times) if end_times else bucket[0].get("end_time"),
                "batch_count": len(bucket),
                "files_compacted": _sum("files_compacted"),
                "files_output": _sum("files_output"),
                "bytes_compacted": _sum("bytes_compacted"),
                "bytes_output": _sum("bytes_output"),
                "duration_seconds": _sum("duration_seconds"),
                "dbus": _sum("dbus"),
                "operation_status": (
                    "FAILED" if any((b.get("operation_status") == "FAILED") for b in bucket)
                    else bucket[0].get("operation_status")
                ),
            }
            # Drop table-size matching for aggregated rows; it's per-commit only
            agg.pop("table_size_before", None)
            agg.pop("table_size_after", None)
            grouped.append(agg)
        i = j

    combined = grouped[:50]

    # Enrich with total-table-size-before/after per commit by walking
    # DESCRIBE HISTORY backward from current DESC DETAIL.sizeInBytes.
    try:
        cur_total = 0
        try:
            dd = execute_sql(client, f"DESCRIBE DETAIL `{catalog}`.`{schema}`.`{table}`")
            dd_rows = rows_as_dicts(dd)
            if dd_rows:
                cur_total = _as_int(dd_rows[0].get("sizeInBytes")) or 0
        except Exception:
            pass

        hist2 = execute_sql(client, f"DESCRIBE HISTORY `{catalog}`.`{schema}`.`{table}` LIMIT 500")
        history_rows = sorted(
            rows_as_dicts(hist2),
            key=lambda r: str(r.get("timestamp") or ""),
            reverse=True,
        )
        # Walk newest -> oldest; size_after of newest = cur_total; size_before = size_after - delta
        running_total = cur_total
        size_by_ts: dict[str, dict[str, int]] = {}
        for row in history_rows:
            ts = str(row.get("timestamp") or "")
            if not ts:
                continue
            raw = row.get("operationMetrics") or "{}"
            try:
                m = json.loads(raw) if isinstance(raw, str) else (raw or {})
            except (ValueError, TypeError):
                m = {}
            added = _as_int(m.get("numAddedBytes")) or 0
            removed = _as_int(m.get("numRemovedBytes")) or 0
            delta = added - removed
            size_after = running_total
            size_before = max(0, running_total - delta)
            size_by_ts[ts[:19]] = {"before": size_before, "after": size_after}
            running_total = size_before

        def _match_size(run_ts: str) -> Optional[dict]:
            if not run_ts:
                return None
            # Exact match on first 19 chars, else closest timestamp within 60s
            key = str(run_ts)[:19]
            if key in size_by_ts:
                return size_by_ts[key]
            # cheap fallback: scan for any ts within 60s
            for k, v in size_by_ts.items():
                if k[:16] == key[:16]:  # same minute
                    return v
            return None

        for r in combined:
            op = (r.get("operation_type") or "").upper()
            if op not in ("OPTIMIZE", "COMPACTION", "CLUSTERING", "VACUUM"):
                continue
            m = _match_size(r.get("start_time") or "") or _match_size(r.get("end_time") or "")
            if m:
                r["table_size_before"] = m["before"]
                r["table_size_after"] = m["after"]
    except Exception:
        pass

    resp: dict = {"runs": combined}
    if spike:
        resp["spike"] = spike
    return resp


@router.get("/running")
def get_running_ops(
    catalog: str,
    schema: str,
    table: str,
    client=Depends(_user_client),
):
    """Find currently-running OPTIMIZE/VACUUM/MERGE statements on this table.

    Optimizations applied:
      - Short time window (15 min) — running ops rarely live longer; shrinks
        the scan a lot vs the previous 4 hr window
      - statement_type whitelist (MERGE + UTILITY) narrows the candidate set
        before the expensive ILIKE runs. OPTIMIZE/VACUUM show up as UTILITY.
      - Warehouse-scoped — if the request carries an X-Warehouse-Id header we
        only scan that warehouse's history. Huge win.
      - Dropped the OR clause's duplicate ILIKE form — the first term covers it.
      - ORDER BY removed (we LIMIT 20, order doesn't matter for this display).
    """
    from ..config import get_warehouse_id
    fq = f"{catalog}.{schema}.{table}"
    wh = get_warehouse_id()

    params = [
        {"name": "fq", "value": fq, "type": "STRING"},
    ]
    wh_clause = ""
    if wh:
        wh_clause = "AND compute.warehouse_id = :wh"
        params.append({"name": "wh", "value": wh, "type": "STRING"})

    sql = f"""
      SELECT statement_type, statement_text, executed_by, execution_status, start_time
      FROM system.query.history
      WHERE execution_status IN ('RUNNING', 'QUEUED')
        AND start_time >= current_timestamp() - INTERVAL 15 MINUTE
        AND statement_type IN ('MERGE', 'UTILITY', 'OTHER')
        {wh_clause}
        AND statement_text ILIKE CONCAT('%', :fq, '%')
      LIMIT 20
    """
    out: list[dict] = []
    try:
        res = execute_sql(client, sql, parameters=params)
        for r in rows_as_dicts(res):
            text = (r.get("statement_text") or "").upper()
            st = r.get("statement_type") or ""
            op = None
            if "OPTIMIZE" in text and "VACUUM" not in text:
                op = "OPTIMIZE"
            elif "VACUUM" in text:
                op = "VACUUM"
            elif st == "MERGE":
                op = "MERGE"
            else:
                op = st or "UNKNOWN"
            out.append({
                "operation_type": op,
                "executed_by": r.get("executed_by"),
                "status": r.get("execution_status"),
                "start_time": str(r.get("start_time") or ""),
            })
    except Exception as e:
        return {"running": [], "error": str(e)}
    return {"running": out}


@router.get("/detail")
def desc_detail(
    catalog: str,
    schema: str,
    table: str,
    client=Depends(_user_client),
):
    """DESC DETAIL — source for num_files, size, avg file size (right now)."""
    full = f"`{catalog}`.`{schema}`.`{table}`"
    try:
        result = execute_sql(client, f"DESCRIBE DETAIL {full}")
        rows = rows_as_dicts(result)
        if not rows:
            return {"detail": None}
        d = rows[0]

        num_files = _as_int(d.get("numFiles")) or 0
        size_bytes = _as_int(d.get("sizeInBytes")) or 0
        avg_file_size = (size_bytes // num_files) if num_files else 0

        # Parse the statistics JSON blob for DV count
        dv_count = 0
        rows_deleted_by_dv = 0
        stats_raw = d.get("statistics")
        if stats_raw:
            try:
                stats = json.loads(stats_raw) if isinstance(stats_raw, str) else stats_raw
                dv_count = _as_int(stats.get("numDeletionVectors")) or 0
                rows_deleted_by_dv = _as_int(stats.get("numRowsDeletedByDeletionVectors")) or 0
            except (ValueError, TypeError):
                pass

        return {
            "detail": d,
            "derived": {
                "num_files": num_files,
                "size_bytes": size_bytes,
                "avg_file_size_bytes": avg_file_size,
                "dv_count": dv_count,
                "rows_deleted_by_dv": rows_deleted_by_dv,
            },
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/trends")
def get_trends(
    catalog: str,
    schema: str,
    table: str,
    days: int = 30,
    client=Depends(_user_client),
):
    """Daily snapshots of files + bytes from system.storage.table_metrics_history,
    per-run DV-removed, and a commit-by-commit table-size series derived from
    DESCRIBE HISTORY (works even when the daily-snapshot table is empty)."""
    out: dict = {"files_bytes": [], "dv_removed": [], "size_history": []}
    # 1. Daily file/byte snapshots
    sql = """
      SELECT snapshot_date, active_files, active_bytes
      FROM system.storage.table_metrics_history
      WHERE catalog_name = :c AND schema_name = :s AND table_name = :t
        AND snapshot_date >= current_date() - INTERVAL :d DAY
      ORDER BY snapshot_date
    """
    try:
        res = execute_sql(client, sql, parameters=[
            {"name": "c", "value": catalog, "type": "STRING"},
            {"name": "s", "value": schema, "type": "STRING"},
            {"name": "t", "value": table, "type": "STRING"},
            {"name": "d", "value": str(days), "type": "INT"},
        ])
        for r in rows_as_dicts(res):
            out["files_bytes"].append({
                "date": str(r.get("snapshot_date") or "")[:10],
                "files": _as_int(r.get("active_files")) or 0,
                "bytes": _as_int(r.get("active_bytes")) or 0,
            })
    except Exception:
        pass

    # 2. DV-removed per OPTIMIZE/COMPACTION from DESCRIBE HISTORY
    try:
        hist = execute_sql(client, f"DESCRIBE HISTORY `{catalog}`.`{schema}`.`{table}` LIMIT 200")
        for row in rows_as_dicts(hist):
            op = (row.get("operation") or "").upper()
            if op != "OPTIMIZE":
                continue
            raw = row.get("operationMetrics") or "{}"
            try:
                m = json.loads(raw) if isinstance(raw, str) else (raw or {})
            except (ValueError, TypeError):
                m = {}
            dvs = _as_int(m.get("numDeletionVectorsRemoved"))
            if dvs is None:
                continue
            out["dv_removed"].append({
                "ts": row.get("timestamp"),
                "dvs_removed": dvs,
                "files_removed": _as_int(m.get("numRemovedFiles")) or 0,
            })
    except Exception:
        pass
    out["dv_removed"].sort(key=lambda r: str(r.get("ts") or ""))

    # 3. Commit-by-commit table size, walking DESCRIBE HISTORY backward from
    #    current DESC DETAIL.sizeInBytes. Works for any table with history.
    try:
        cur_total = 0
        try:
            dd = execute_sql(client, f"DESCRIBE DETAIL `{catalog}`.`{schema}`.`{table}`")
            dd_rows = rows_as_dicts(dd)
            if dd_rows:
                cur_total = _as_int(dd_rows[0].get("sizeInBytes")) or 0
        except Exception:
            pass

        hist = execute_sql(client, f"DESCRIBE HISTORY `{catalog}`.`{schema}`.`{table}` LIMIT 500")
        rows_newest_first = sorted(
            rows_as_dicts(hist),
            key=lambda r: str(r.get("timestamp") or ""),
            reverse=True,
        )
        # Newest row's size_after = current total; walk backward computing size_before.
        size_history: list[dict] = []
        running = cur_total
        for row in rows_newest_first:
            ts = row.get("timestamp")
            if not ts:
                continue
            raw = row.get("operationMetrics") or "{}"
            try:
                m = json.loads(raw) if isinstance(raw, str) else (raw or {})
            except (ValueError, TypeError):
                m = {}
            added = _as_int(m.get("numAddedBytes")) or 0
            removed = _as_int(m.get("numRemovedBytes")) or 0
            size_history.append({"ts": str(ts), "bytes": running})
            running = max(0, running - (added - removed))
        out["size_history"] = list(reversed(size_history))
    except Exception:
        pass

    return out


@router.get("/merges")
def get_merge_activity(
    catalog: str,
    schema: str,
    table: str,
    hours: int = 24,
    client=Depends(_user_client),
):
    """MERGE activity on this table from system.query.history.

    Counts total MERGE statements, failed ones, and classifies failures by
    error pattern (DELTA_CONCURRENT_* = conflict, other = other).
    """
    fq = f"{catalog}.{schema}.{table}"
    sql = """
      SELECT
        execution_status,
        error_message,
        start_time,
        total_duration_ms
      FROM system.query.history
      WHERE statement_type = 'MERGE'
        AND start_time >= current_timestamp() - make_interval(0, 0, 0, 0, :h, 0, 0)
        AND (
          statement_text ILIKE CONCAT('%', :fq, '%')
          OR statement_text ILIKE CONCAT('%`', :cat, '`.`', :sch, '`.`', :tbl, '`%')
        )
      ORDER BY start_time DESC
      LIMIT 500
    """
    out = {
        "window_hours": hours,
        "total": 0,
        "successful": 0,
        "failed": 0,
        "conflicts": 0,
        "recent": [],
    }
    try:
        res = execute_sql(client, sql, parameters=[
            {"name": "h", "value": str(hours), "type": "INT"},
            {"name": "fq", "value": fq, "type": "STRING"},
            {"name": "cat", "value": catalog, "type": "STRING"},
            {"name": "sch", "value": schema, "type": "STRING"},
            {"name": "tbl", "value": table, "type": "STRING"},
        ])
        for r in rows_as_dicts(res):
            out["total"] += 1
            status = r.get("execution_status") or ""
            err = r.get("error_message") or ""
            if status == "FINISHED":
                out["successful"] += 1
            else:
                out["failed"] += 1
                if "DELTA_CONCURRENT" in err or "DELTA_DUPLICATE_ACTIONS_FOUND" in err:
                    out["conflicts"] += 1
            if len(out["recent"]) < 8:
                out["recent"].append({
                    "status": status,
                    "error": (err[:200] if err else None),
                    "start_time": str(r.get("start_time") or ""),
                    "duration_ms": _as_int(r.get("total_duration_ms")),
                })
        out["conflict_rate"] = round(out["conflicts"] / out["total"], 3) if out["total"] else 0.0
    except Exception as e:
        out["error"] = str(e)
    return out


@router.get("/health")
def table_health(
    catalog: str,
    schema: str,
    table: str,
    client=Depends(_user_client),
):
    """Composite health rollup: combines DESC DETAIL + PO run history.

    TODO: Wire the full badge ruleset from the spec:
      - Red: PO quarantined | VACUUM > 30d | OPTIMIZE failure rate > 30%
      - Amber: unclustered_bytes > 20% | avg file size dropping 3d | failure 10-30%
      - Green: otherwise
    For now, returns partial signals + TODO flag.
    """
    try:
        detail = desc_detail(catalog, schema, table, client)
        runs_resp = get_po_runs(catalog, schema, table, 30, client)
        runs = runs_resp["runs"]

        # PO operation_types: CLUSTERING, COMPACTION, VACUUM, ANALYZE, DATA_SKIPPING_COLUMN_SELECTION
        # Manual (DESCRIBE HISTORY): OPTIMIZE, VACUUM, MERGE
        # Status values: SUCCESSFUL, FAILED
        OPTIMIZE_OPS = {"CLUSTERING", "COMPACTION", "OPTIMIZE"}
        last_optimize = next(
            (r for r in runs
             if r.get("operation_type") in OPTIMIZE_OPS
             and r.get("operation_status") == "SUCCESSFUL"),
            None,
        )
        last_vacuum = next(
            (r for r in runs
             if r.get("operation_type") == "VACUUM"
             and r.get("operation_status") == "SUCCESSFUL"),
            None,
        )

        # Simple failure-rate calc across optimize-family ops
        optimize_runs = [r for r in runs if r.get("operation_type") in OPTIMIZE_OPS][:10]
        failed = sum(1 for r in optimize_runs if r.get("operation_status") == "FAILED")
        failure_rate = (failed / len(optimize_runs)) if optimize_runs else 0.0

        # Naive badge (TODO: full rule eval)
        badge = "green"
        reasons = []
        if failure_rate > 0.30:
            badge = "red"; reasons.append(f"OPTIMIZE failure rate {failure_rate:.0%}")
        elif failure_rate >= 0.10:
            badge = "amber"; reasons.append(f"OPTIMIZE failure rate {failure_rate:.0%}")

        # Trend deltas: compare current DESC DETAIL to a snapshot ~7d ago
        trend = {"files_pct": None, "bytes_pct": None, "avg_size_pct": None}
        try:
            snap = execute_sql(
                client,
                """
                SELECT active_files, active_bytes
                FROM system.storage.table_metrics_history
                WHERE catalog_name = :c AND schema_name = :s AND table_name = :t
                  AND snapshot_date = current_date() - INTERVAL 7 DAY
                LIMIT 1
                """,
                parameters=[
                    {"name": "c", "value": catalog, "type": "STRING"},
                    {"name": "s", "value": schema, "type": "STRING"},
                    {"name": "t", "value": table, "type": "STRING"},
                ],
            )
            srows = rows_as_dicts(snap)
            if srows:
                prev_files = _as_int(srows[0].get("active_files")) or 0
                prev_bytes = _as_int(srows[0].get("active_bytes")) or 0
                cur = detail.get("derived") or {}
                cur_files = cur.get("num_files") or 0
                cur_bytes = cur.get("size_bytes") or 0
                if prev_files:
                    trend["files_pct"] = round((cur_files - prev_files) / prev_files * 100, 1)
                if prev_bytes:
                    trend["bytes_pct"] = round((cur_bytes - prev_bytes) / prev_bytes * 100, 1)
                if prev_files and prev_bytes:
                    prev_avg = prev_bytes // prev_files
                    cur_avg = (cur.get("avg_file_size_bytes") or 0)
                    if prev_avg:
                        trend["avg_size_pct"] = round((cur_avg - prev_avg) / prev_avg * 100, 1)
        except Exception:
            pass

        # Files-since-last-optimize as proxy for "unclustered bytes"
        files_since_last_optimize = None
        bytes_since_last_optimize = None
        try:
            if last_optimize and last_optimize.get("start_time"):
                pivot = last_optimize["start_time"]
                hist = execute_sql(
                    client,
                    f"DESCRIBE HISTORY `{catalog}`.`{schema}`.`{table}` LIMIT 200",
                )
                f = 0
                b = 0
                for row in rows_as_dicts(hist):
                    ts = str(row.get("timestamp") or "")
                    if ts <= str(pivot):
                        continue
                    op = (row.get("operation") or "").upper()
                    if op not in ("WRITE", "MERGE", "STREAMING UPDATE", "UPDATE"):
                        continue
                    raw = row.get("operationMetrics") or "{}"
                    try:
                        m = json.loads(raw) if isinstance(raw, str) else (raw or {})
                    except (ValueError, TypeError):
                        m = {}
                    f += _as_int(m.get("numAddedFiles")) or 0
                    b += _as_int(m.get("numAddedBytes")) or 0
                files_since_last_optimize = f
                bytes_since_last_optimize = b
        except Exception:
            pass

        # Resolve Predictive Optimization state (includes inheritance)
        po_state = {"enabled": None, "raw": None, "inherited": False}
        try:
            de = execute_sql(client, f"DESCRIBE EXTENDED `{catalog}`.`{schema}`.`{table}`")
            for row in de["rows"]:
                if not row or not row[0]:
                    continue
                if str(row[0]).strip().lower() == "predictive optimization":
                    raw = str(row[1] or "") if len(row) > 1 else ""
                    po_state["raw"] = raw
                    up = raw.upper()
                    po_state["enabled"] = up.startswith("ENABLE")
                    po_state["inherited"] = "INHERITED" in up
                    break
        except Exception:
            pass

        return {
            "badge": badge,
            "reasons": reasons,
            "last_optimize": last_optimize,
            "last_vacuum": last_vacuum,
            "detail": detail.get("derived"),
            "trend": trend,
            "unclustered_proxy": {
                "files_since_last_optimize": files_since_last_optimize,
                "bytes_since_last_optimize": bytes_since_last_optimize,
            },
            "po_state": po_state,
            "spike": runs_resp.get("spike"),
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
