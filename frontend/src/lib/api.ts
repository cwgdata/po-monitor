// Thin wrapper around fetch that prefixes /api and JSON-parses.
// User auth (X-Forwarded-Access-Token) is injected automatically by the
// Databricks Apps proxy — no client-side action needed.

// The currently-selected SQL warehouse. Every request carries this as
// X-Warehouse-Id so the backend routes queries to it (set via
// setActiveWarehouseId from the sidebar).
let _activeWarehouseId: string | null = null;
export function setActiveWarehouseId(id: string | null) {
  _activeWarehouseId = id || null;
}
export function getActiveWarehouseId(): string | null {
  return _activeWarehouseId;
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const headers: Record<string, string> = {
    'Content-Type': 'application/json',
    ...((init?.headers as Record<string, string>) || {}),
  };
  if (_activeWarehouseId) headers['X-Warehouse-Id'] = _activeWarehouseId;
  const res = await fetch(path, {
    ...init,
    headers,
  });
  if (!res.ok) {
    // Token expired / unauthenticated — bounce to the app root which triggers
    // the Databricks Apps OAuth proxy to re-auth, rather than letting the
    // user see stale errors.
    if (res.status === 401 || res.status === 403) {
      let msg = `${res.status} ${res.statusText}`;
      try {
        const body = await res.json();
        msg = body.detail || body.error || msg;
      } catch {}
      // Only redirect when the error clearly looks like an auth failure,
      // not a missing-permission / resource error (checked via message text).
      const looksLikeAuth = /invalid token|expired|unauthenticated|token expired|scope/i.test(msg) || res.status === 401;
      if (looksLikeAuth) {
        // Force fresh OAuth prompt
        window.location.href = '/sign-out';
        throw new Error('Session expired — redirecting to sign in.');
      }
      throw new Error(msg);
    }
    let msg = `${res.status} ${res.statusText}`;
    try {
      const body = await res.json();
      msg = body.detail || body.error || msg;
    } catch {}
    throw new Error(msg);
  }
  return res.json() as Promise<T>;
}

export const api = {
  health: () => request<{ ok: boolean }>('/api/health'),

  listCatalogs: () => request<{ catalogs: string[] }>('/api/catalog/catalogs'),
  listSchemas: (catalog: string) =>
    request<{ schemas: string[] }>(`/api/catalog/schemas?catalog=${encodeURIComponent(catalog)}`),
  listTables: (catalog: string, schema: string, managedOnly = true) =>
    request<{ tables: Array<{ table_name: string; data_source_format: string }> }>(
      `/api/catalog/tables?catalog=${encodeURIComponent(catalog)}&schema=${encodeURIComponent(schema)}&managed_only=${managedOnly}`,
    ),
  whoami: () =>
    request<{ email: string | null; name: string | null }>('/api/catalog/whoami'),

  getCardCache: (catalog: string, schema: string, table: string) =>
    request<{ payload: any | null; updated_at: string | null }>(
      `/api/card-cache?catalog=${c(catalog)}&schema=${c(schema)}&table=${c(table)}`,
    ),
  saveCardCache: (body: { catalog: string; schema: string; table: string; payload: any }) =>
    request<{ ok: boolean; reason?: string }>(
      '/api/card-cache', { method: 'POST', body: JSON.stringify(body) }),

  sendFeedback: (body: { subject: string; message: string; app_url?: string; user_agent?: string }) =>
    request<{ status: string; feedback_id: string; delivered: boolean }>(
      '/api/feedback',
      { method: 'POST', body: JSON.stringify(body) },
    ),

  getPoRuns: (catalog: string, schema: string, table: string, lookback = 30) =>
    request<{ runs: PoRun[]; spike?: string }>(
      `/api/po/runs?catalog=${c(catalog)}&schema=${c(schema)}&table=${c(table)}&lookback_days=${lookback}`,
    ),
  getDetail: (catalog: string, schema: string, table: string) =>
    request<{ detail: any; derived: { num_files: number; size_bytes: number; avg_file_size_bytes: number } }>(
      `/api/po/detail?catalog=${c(catalog)}&schema=${c(schema)}&table=${c(table)}`,
    ),
  getHealth: (catalog: string, schema: string, table: string) =>
    request<HealthResponse>(
      `/api/po/health?catalog=${c(catalog)}&schema=${c(schema)}&table=${c(table)}`,
    ),
  getGroupHealth: (catalog: string, schema?: string | null, max_tables = 50) => {
    const q = new URLSearchParams({ catalog, max_tables: String(max_tables) });
    if (schema) q.set('schema', schema);
    return request<GroupHealthResponse>(`/api/po/group_health?${q.toString()}`);
  },
  getTrends: (catalog: string, schema: string, table: string, days = 30) =>
    request<{
      files_bytes: Array<{ date: string; files: number; bytes: number }>;
      dv_removed: Array<{ ts: string; dvs_removed: number; files_removed: number }>;
      size_history: Array<{ ts: string; bytes: number }>;
    }>(`/api/po/trends?catalog=${c(catalog)}&schema=${c(schema)}&table=${c(table)}&days=${days}`),
  getRunning: (catalog: string, schema: string, table: string) =>
    request<{ running: Array<{ operation_type: string; executed_by: string; status: string; start_time: string }>; error?: string }>(
      `/api/po/running?catalog=${c(catalog)}&schema=${c(schema)}&table=${c(table)}`,
    ),
  getMerges: (catalog: string, schema: string, table: string, hours = 24) =>
    request<{
      window_hours: number;
      total: number;
      successful: number;
      failed: number;
      conflicts: number;
      conflict_rate: number;
      recent: Array<{ status: string; error: string | null; start_time: string; duration_ms: number | null }>;
      error?: string;
    }>(`/api/po/merges?catalog=${c(catalog)}&schema=${c(schema)}&table=${c(table)}&hours=${hours}`),

  runOptimize: (body: TableRef) =>
    request<{ status: string; target: string; statement_id: string; state?: string; done?: boolean }>(
      '/api/actions/optimize', { method: 'POST', body: JSON.stringify(body) }),
  runVacuum: (body: TableRef & { mode: 'LITE' | 'FULL' }) =>
    request<{ status: string; target: string; statement_id: string; state?: string; done?: boolean; mode: string }>(
      '/api/actions/vacuum', { method: 'POST', body: JSON.stringify(body) }),
  togglePO: (body: TableRef & { enabled: boolean }) =>
    request('/api/actions/toggle_po', { method: 'POST', body: JSON.stringify(body) }),
  forceTrigger: (body: TableRef) =>
    request('/api/actions/force_trigger', { method: 'POST', body: JSON.stringify(body) }),
  schedule: (body: TableRef & { operation: string; cron: string; timezone_id: string }) =>
    request('/api/actions/schedule', { method: 'POST', body: JSON.stringify(body) }),

  getAudit: () => request<{ entries: AuditEntry[] }>('/api/actions/audit'),

  listAlerts: () => request<{ rules: AlertRule[]; active: any[] }>('/api/alerts'),
  createAlert: (rule: any) =>
    request<{ ok: boolean; rule_id: string }>(
      '/api/alerts', { method: 'POST', body: JSON.stringify(rule) }),
  patchAlert: (rule_id: string, patch: Partial<AlertRule>) =>
    request<{ ok: boolean; rule: AlertRule }>(
      `/api/alerts/${encodeURIComponent(rule_id)}`,
      { method: 'PATCH', body: JSON.stringify(patch) }),
  deleteAlert: (rule_id: string) =>
    request<{ ok: boolean }>(
      `/api/alerts/${encodeURIComponent(rule_id)}`, { method: 'DELETE' }),
  testAlert: (rule_id: string) =>
    request<{ ok: boolean; results: AlertTestResult[]; count: number }>(
      `/api/alerts/${encodeURIComponent(rule_id)}/test`, { method: 'POST' }),
  listAlertEvents: (params?: { rule_id?: string; limit?: number }) => {
    const qs = new URLSearchParams();
    if (params?.rule_id) qs.set('rule_id', params.rule_id);
    if (params?.limit) qs.set('limit', String(params.limit));
    const q = qs.toString();
    return request<{ events: AlertEvent[] }>(`/api/alerts/events${q ? '?' + q : ''}`);
  },

  getConfig: () => request<AppConfig>('/api/config'),
  patchConfig: (patch: Partial<AppConfig> & { thresholds?: Record<string, number> }) =>
    request<AppConfig>('/api/config', { method: 'PATCH', body: JSON.stringify(patch) }),
  listWarehouses: () =>
    request<{ warehouses: Array<{ id: string; name: string; state?: string; size?: string }> }>(
      '/api/config/warehouses',
    ),

  // --- Dashboards (saved per-user selections) ---
  listDashboards: () =>
    request<{ configs: SavedDashboard[] }>('/api/dashboards'),
  saveDashboard: (body: { name: string; tables: TableRef[] }) =>
    request<{ ok: boolean; config_id: string; name: string; tables: TableRef[] }>(
      '/api/dashboards', { method: 'POST', body: JSON.stringify(body) }),
  loadDashboard: (config_id: string) =>
    request<SavedDashboard>(`/api/dashboards/${encodeURIComponent(config_id)}/load`, { method: 'POST' }),
  deleteDashboard: (config_id: string) =>
    request<{ ok: boolean }>(`/api/dashboards/${encodeURIComponent(config_id)}`, { method: 'DELETE' }),

  // --- Schedules ---
  listSchedules: () =>
    request<{ schedules: ScheduleRow[] }>('/api/schedules'),
  createSchedule: (body: NewSchedule) =>
    request<ScheduleRow>('/api/schedules', { method: 'POST', body: JSON.stringify(body) }),
  patchSchedule: (id: string, patch: Partial<NewSchedule>) =>
    request<ScheduleRow>(`/api/schedules/${encodeURIComponent(id)}`,
      { method: 'PATCH', body: JSON.stringify(patch) }),
  deleteSchedule: (id: string) =>
    request<{ ok: boolean }>(`/api/schedules/${encodeURIComponent(id)}`, { method: 'DELETE' }),
  runScheduleNow: (id: string) =>
    request<{ status: string; schedule_id: string; statement_id?: string }>(
      `/api/schedules/${encodeURIComponent(id)}/run`, { method: 'POST' }),
};

export type AlertRuleType =
  | 'PO_QUARANTINED'
  | 'OPTIMIZE_FAILURE_RATE'
  | 'VACUUM_STALE'
  | 'UNCLUSTERED_BYTES'
  | 'AVG_FILE_SIZE_DROP'
  | 'MERGE_CONFLICT_SPIKE';

export type AlertRule = {
  rule_id: string;
  catalog?: string | null;
  schema_name?: string | null;
  table_name?: string | null;
  rule_type: AlertRuleType;
  threshold: number;
  lookback_minutes: number;
  enabled: boolean;
  slack_webhook?: string | null;
  slack_webhook_masked?: string | null;
  email?: string | null;
  created_by?: string | null;
  created_at?: string | null;
  updated_at?: string | null;
  last_evaluated_at?: string | null;
  last_fired_at?: string | null;
  last_status?: 'OK' | 'FIRED' | 'ERROR' | null;
  last_error?: string | null;
};

export type AlertEvent = {
  event_id: string;
  rule_id: string;
  fired_at: string | null;
  catalog?: string | null;
  schema_name?: string | null;
  table_name?: string | null;
  rule_type: AlertRuleType;
  threshold: number;
  observed_value: number;
  message: string | null;
  delivery: string | null;
  delivery_error: string | null;
};

export type AlertTestResult = {
  triggered: boolean;
  observed: number;
  message: string;
  target: string;
  would_dispatch: {
    slack: string;
    email: string;
    summary: string;
    error?: string | null;
    payload?: { text: string };
    webhook_set?: boolean;
    email_set?: boolean;
  };
};

export type SavedDashboard = {
  config_id: string;
  name: string;
  tables: TableRef[];
  created_at?: string;
  updated_at?: string;
};

export type NewSchedule = {
  catalog: string;
  schema: string;
  table: string;
  operation: 'OPTIMIZE' | 'VACUUM_LITE' | 'VACUUM_FULL';
  schedule_type: 'cron' | 'trigger';
  cron?: string;
  timezone?: string;
  poll_interval_seconds?: number;
  min_interval_seconds?: number;
  warehouse_id: string;
  enabled: boolean;
};

export type ScheduleRow = NewSchedule & {
  schedule_id: string;
  user_email?: string;
  created_at?: string;
  updated_at?: string;
  last_run_at?: string | null;
  last_checked_at?: string | null;
  last_run_status?: string | null;
  last_run_error?: string | null;
  last_run_statement_id?: string | null;
};

const c = encodeURIComponent;

export type TableRef = { catalog: string; schema: string; table: string };

export type PoRun = {
  source: 'po' | 'manual';
  operation_type: string;
  operation_status: string;
  start_time: string;
  end_time?: string;
  duration_seconds?: number;
  files_compacted?: number;
  files_output?: number;
  bytes_compacted?: number;
  bytes_output?: number;
  files_deleted?: number;
  bytes_deleted?: number;
  staleness_reduced_pct?: number;
  dbus?: number;
  user?: string;
  table_size_before?: number;
  table_size_after?: number;
  batch_count?: number;
};

export type HealthResponse = {
  badge: 'green' | 'amber' | 'red' | 'unknown';
  reasons: string[];
  last_optimize?: PoRun;
  last_vacuum?: PoRun;
  vacuum_age_days?: number | null;
  failure_rate?: number;
  detail?: {
    num_files: number;
    size_bytes: number;
    avg_file_size_bytes: number;
    dv_count?: number;
    rows_deleted_by_dv?: number;
  };
  trend?: {
    files_pct: number | null;
    bytes_pct: number | null;
    avg_size_pct: number | null;
  };
  unclustered_proxy?: {
    files_since_last_optimize: number | null;
    bytes_since_last_optimize: number | null;
    ratio?: number | null;
  };
  merges?: {
    window_hours?: number;
    total?: number;
    successful?: number;
    failed?: number;
    conflicts?: number;
    conflict_rate?: number;
    error?: string;
  } | null;
  po_state?: {
    enabled: boolean | null;
    raw: string | null;
    inherited: boolean;
  };
  spike?: string;
};

export type GroupRef = {
  kind: 'schema' | 'catalog';
  catalog: string;
  schema?: string | null;
};

export type GroupHealthResponse = {
  group: GroupRef;
  badge: 'green' | 'amber' | 'red' | 'unknown';
  total_tables: number;
  evaluated_tables: number;
  truncated: boolean;
  max_tables: number;
  counts: { red: number; amber: number; green: number; unknown: number; error: number };
  totals: { size_bytes: number; num_files: number; avg_file_size_bytes: number };
  avg_failure_rate: number;
  last_optimize_max: string | null;
  last_vacuum_max: string | null;
  offenders: Array<{
    catalog: string;
    schema: string;
    table: string;
    badge: string;
    reasons: string[];
    vacuum_age_days?: number | null;
    failure_rate?: number;
  }>;
};

export type AuditEntry = {
  ts: string;
  user: string;
  action: string;
  target: string;
  result: string;
  meta?: any;
};

export type AppConfig = {
  workspace_host: string;
  warehouse_id: string | null;
  warehouse_id_override?: string | null;
  is_databricks_app: boolean;
  default_catalog?: string;
  default_schema?: string;
  slack_webhook_url?: string;
  alert_email_to?: string;
  thresholds: Record<string, number>;
};
