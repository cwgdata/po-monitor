-- SPIKE: Predictive Optimization run history from system tables.
--
-- TODO: Verify exact table name. As of 2026-04, the PO system table
-- has shipped under system.storage.predictive_optimization_operations_history
-- but earlier docs referenced system.lakeflow.optimizer_runs. Probe both:
--
--   SHOW TABLES IN system.storage LIKE '*predictive*';
--   SHOW TABLES IN system.lakeflow LIKE '*optim*';
--
-- Columns used below are best-guess based on public docs; adjust after probe.
--
-- Query params (Statement Execution API named params):
--   :catalog, :schema, :table  -- target table's 3-part name
--   :lookback_days             -- default 30
SELECT
  operation_id,
  operation_type,          -- OPTIMIZE | VACUUM_LITE | VACUUM_FULL | ANALYZE
  operation_status,        -- SUCCEEDED | FAILED | SKIPPED | QUARANTINED
  start_time,
  end_time,
  (unix_timestamp(end_time) - unix_timestamp(start_time)) AS duration_seconds,
  metrics:num_files_compacted::bigint      AS files_compacted,
  metrics:num_deletion_vectors_removed::bigint AS dvs_removed,
  metrics:bytes_written::bigint            AS bytes_written,
  error_message
FROM system.storage.predictive_optimization_operations_history
WHERE catalog_name = :catalog
  AND schema_name  = :schema
  AND table_name   = :table
  AND start_time >= current_timestamp() - INTERVAL :lookback_days DAYS
ORDER BY start_time DESC
LIMIT 50;
