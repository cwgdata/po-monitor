-- List managed Iceberg tables for a given catalog.schema.
-- UC exposes table format via information_schema.tables.data_source_format.
-- For managed Iceberg specifically, data_source_format = 'ICEBERG' and
-- table_type = 'MANAGED'.
--
-- NOTE: If a workspace still uses the older UC metastore layout where only
-- DELTA + external ICEBERG is exposed, this returns empty — surface that
-- cleanly in the UI.
SELECT
  table_catalog,
  table_schema,
  table_name,
  data_source_format,
  table_type,
  created,
  last_altered
FROM system.information_schema.tables
WHERE table_catalog = :catalog
  AND table_schema  = :schema
  AND table_type    = 'MANAGED'
  AND data_source_format IN ('ICEBERG', 'DELTA')  -- DELTA included since PO covers both; UI filters to ICEBERG by default
ORDER BY table_name;
