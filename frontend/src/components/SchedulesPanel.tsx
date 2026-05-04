import { useEffect, useState } from 'react';
import { api, type ScheduleRow, type NewSchedule } from '../lib/api';
import { Toggle } from './Toggle';

type Props = {
  userEmail: string | null;
};

const EMPTY_FORM: NewSchedule = {
  catalog: '',
  schema: '',
  table: '',
  operation: 'OPTIMIZE',
  schedule_type: 'cron',
  cron: '0 2 * * *',
  timezone: 'UTC',
  poll_interval_seconds: 300,
  min_interval_seconds: 1800,
  warehouse_id: '',
  enabled: true,
};

function agoString(iso: string | null | undefined): string {
  if (!iso) return '—';
  const d = Date.parse(iso);
  if (!Number.isFinite(d)) return '—';
  const s = Math.floor((Date.now() - d) / 1000);
  if (s < 5) return 'just now';
  if (s < 60) return `${s}s ago`;
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return `${Math.floor(s / 86400)}d ago`;
}

export function SchedulesPanel({ userEmail }: Props) {
  const [rows, setRows] = useState<ScheduleRow[]>([]);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [modalOpen, setModalOpen] = useState(false);
  const [form, setForm] = useState<NewSchedule>(EMPTY_FORM);
  const [saving, setSaving] = useState(false);
  const [editingId, setEditingId] = useState<string | null>(null);

  // Cascading selects for the modal
  const [catalogs, setCatalogs] = useState<string[]>([]);
  const [schemas, setSchemas] = useState<string[]>([]);
  const [tables, setTables] = useState<string[]>([]);
  const [warehouses, setWarehouses] = useState<Array<{ id: string; name: string; state?: string }>>([]);

  const refresh = async () => {
    setLoading(true);
    try {
      const r = await api.listSchedules();
      setRows(r.schedules);
    } catch (e: any) {
      setErr(e.message);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    refresh();
    // Keep "last run" columns live without blocking clicks
    const id = setInterval(refresh, 30_000);
    return () => clearInterval(id);
  }, []);

  // Load catalogs + warehouses once, schemas/tables on demand
  useEffect(() => {
    api.listCatalogs().then((r) => setCatalogs(r.catalogs)).catch(() => {});
    api.listWarehouses().then((r) => setWarehouses(r.warehouses)).catch(() => {});
  }, []);

  useEffect(() => {
    setSchemas([]);
    setTables([]);
    if (form.catalog) {
      api.listSchemas(form.catalog).then((r) => setSchemas(r.schemas)).catch(() => {});
    }
  }, [form.catalog]);

  useEffect(() => {
    setTables([]);
    if (form.catalog && form.schema) {
      api.listTables(form.catalog, form.schema, true)
        .then((r) => setTables(r.tables.map((t) => t.table_name)))
        .catch(() => {});
    }
  }, [form.catalog, form.schema]);

  const warehouseName = (id?: string) => warehouses.find((w) => w.id === id)?.name || id || '—';

  const openCreate = () => {
    const defaultWh = warehouses[0]?.id || '';
    setForm({ ...EMPTY_FORM, warehouse_id: defaultWh });
    setEditingId(null);
    setErr(null);
    setModalOpen(true);
  };

  const openEdit = (row: ScheduleRow) => {
    setForm({
      catalog: row.catalog,
      schema: row.schema,
      table: row.table,
      operation: row.operation as NewSchedule['operation'],
      schedule_type: row.schedule_type as NewSchedule['schedule_type'],
      cron: row.cron || '0 2 * * *',
      timezone: row.timezone || 'UTC',
      poll_interval_seconds: row.poll_interval_seconds ?? 300,
      min_interval_seconds: row.min_interval_seconds ?? 1800,
      warehouse_id: row.warehouse_id || '',
      enabled: row.enabled,
    });
    setEditingId(row.schedule_id);
    setErr(null);
    setModalOpen(true);
  };

  const save = async () => {
    if (!form.catalog || !form.schema || !form.table) {
      setErr('Pick a catalog, schema, and table');
      return;
    }
    if (!form.warehouse_id) {
      setErr('Pick a warehouse');
      return;
    }
    if (form.schedule_type === 'cron' && (!form.cron || form.cron.split(' ').length !== 5)) {
      setErr('Cron must be a 5-field expression (e.g. "0 2 * * *")');
      return;
    }
    setSaving(true);
    setErr(null);
    try {
      if (editingId) {
        await api.patchSchedule(editingId, form);
      } else {
        await api.createSchedule(form);
      }
      setModalOpen(false);
      setEditingId(null);
      refresh();
    } catch (e: any) {
      setErr(e.message);
    } finally {
      setSaving(false);
    }
  };

  const toggle = async (row: ScheduleRow) => {
    try {
      await api.patchSchedule(row.schedule_id, { enabled: !row.enabled });
      refresh();
    } catch (e: any) {
      setErr(e.message);
    }
  };

  const runNow = async (row: ScheduleRow) => {
    try {
      await api.runScheduleNow(row.schedule_id);
      refresh();
    } catch (e: any) {
      setErr(`Run failed: ${e.message}`);
    }
  };

  const del = async (row: ScheduleRow) => {
    if (!confirm(`Delete schedule for ${row.catalog}.${row.schema}.${row.table}?`)) return;
    try {
      await api.deleteSchedule(row.schedule_id);
      refresh();
    } catch (e: any) {
      setErr(e.message);
    }
  };

  return (
    <div>
      <div className="stub" style={{ marginBottom: 12, borderStyle: 'solid' }}>
        <strong>Heads up:</strong>{' '}
        Schedules run as the app's service principal.
        Ensure the SP has <code>MODIFY</code> on each target table or OPTIMIZE/VACUUM will fail.
        If the app is restarted, in-flight ticks pick up on the next cycle (~30s).
      </div>

      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 12 }}>
        <h3 style={{ margin: 0 }}>Schedules ({rows.length})</h3>
        <button className="primary" onClick={openCreate} disabled={!userEmail}>
          + Create schedule
        </button>
      </div>

      {err && <div className="spike" style={{ marginBottom: 12 }}>{err}</div>}

      <div className="card">
        {loading && rows.length === 0 ? (
          <div className="muted">Loading…</div>
        ) : rows.length === 0 ? (
          <div className="empty-state">
            No schedules yet. Create one to auto-run OPTIMIZE / VACUUM on a cron or on data-change triggers.
          </div>
        ) : (
          <div className="table-scroll">
            <table className="data">
              <thead>
                <tr>
                  <th>Target</th>
                  <th>Operation</th>
                  <th>Type</th>
                  <th>Schedule</th>
                  <th>Warehouse</th>
                  <th>Enabled</th>
                  <th>Last run</th>
                  <th>Last check</th>
                  <th>Status</th>
                  <th>Actions</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((r) => (
                  <tr key={r.schedule_id}>
                    <td title={`${r.catalog}.${r.schema}.${r.table}`}>
                      <span className="muted">{r.catalog}.{r.schema}.</span>{r.table}
                    </td>
                    <td>{r.operation}</td>
                    <td>{r.schedule_type}</td>
                    <td>
                      {r.schedule_type === 'cron'
                        ? <code>{r.cron} {r.timezone !== 'UTC' ? r.timezone : ''}</code>
                        : <span className="muted">poll {r.poll_interval_seconds}s / min {r.min_interval_seconds}s</span>}
                    </td>
                    <td>{warehouseName(r.warehouse_id)}</td>
                    <td>
                      <Toggle checked={r.enabled} onChange={() => toggle(r)} />
                    </td>
                    <td title={r.last_run_at || ''}>{agoString(r.last_run_at)}</td>
                    <td title={r.last_checked_at || ''}>{agoString(r.last_checked_at)}</td>
                    <td title={r.last_run_error || ''}>
                      {r.last_run_status
                        ? <span className={
                            r.last_run_status.toLowerCase() === 'error' ? 'trend-up' :
                            r.last_run_status.toLowerCase() === 'succeeded' || r.last_run_status.toLowerCase() === 'submitted' ? 'trend-down' :
                            'muted'
                          }>{r.last_run_status}</span>
                        : <span className="muted">—</span>}
                    </td>
                    <td>
                      <div style={{ display: 'flex', gap: 4 }}>
                        <button onClick={() => openEdit(r)} style={{ padding: '2px 8px', fontSize: 11 }}>Edit</button>
                        <button onClick={() => runNow(r)} style={{ padding: '2px 8px', fontSize: 11 }}>Run now</button>
                        <button onClick={() => del(r)} style={{ padding: '2px 8px', fontSize: 11 }}>×</button>
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {modalOpen && (
        <div className="modal-backdrop" onClick={(e) => { if (e.target === e.currentTarget) setModalOpen(false); }}>
          <div className="modal">
            <h3>{editingId ? 'Edit schedule' : 'Create schedule'}</h3>

            <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr 1fr', gap: 8 }}>
              <div>
                <div className="section-label">Catalog</div>
                <select value={form.catalog} onChange={(e) => setForm({ ...form, catalog: e.target.value, schema: '', table: '' })}>
                  <option value="">Select…</option>
                  {catalogs.map((c) => <option key={c} value={c}>{c}</option>)}
                </select>
              </div>
              <div>
                <div className="section-label">Schema</div>
                <select value={form.schema} disabled={!form.catalog} onChange={(e) => setForm({ ...form, schema: e.target.value, table: '' })}>
                  <option value="">Select…</option>
                  {schemas.map((s) => <option key={s} value={s}>{s}</option>)}
                </select>
              </div>
              <div>
                <div className="section-label">Table</div>
                <select value={form.table} disabled={!form.schema} onChange={(e) => setForm({ ...form, table: e.target.value })}>
                  <option value="">Select…</option>
                  {tables.map((t) => <option key={t} value={t}>{t}</option>)}
                </select>
              </div>
            </div>

            <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 8, marginTop: 8 }}>
              <div>
                <div className="section-label">Operation</div>
                <select value={form.operation} onChange={(e) => setForm({ ...form, operation: e.target.value as any })}>
                  <option value="OPTIMIZE">OPTIMIZE</option>
                  <option value="VACUUM_LITE">VACUUM LITE</option>
                  <option value="VACUUM_FULL">VACUUM FULL</option>
                </select>
              </div>
              <div>
                <div className="section-label">Warehouse</div>
                <select value={form.warehouse_id} onChange={(e) => setForm({ ...form, warehouse_id: e.target.value })}>
                  <option value="">Select…</option>
                  {warehouses.map((w) => <option key={w.id} value={w.id}>{w.name}</option>)}
                </select>
              </div>
            </div>

            <div className="section-label">Type</div>
            <div style={{ display: 'flex', gap: 16 }}>
              <label style={{ display: 'flex', gap: 4, alignItems: 'center' }}>
                <input type="radio" name="stype" checked={form.schedule_type === 'cron'} onChange={() => setForm({ ...form, schedule_type: 'cron' })} />
                <span>Cron</span>
              </label>
              <label style={{ display: 'flex', gap: 4, alignItems: 'center' }}>
                <input type="radio" name="stype" checked={form.schedule_type === 'trigger'} onChange={() => setForm({ ...form, schedule_type: 'trigger' })} />
                <span>Data change trigger</span>
              </label>
            </div>

            {form.schedule_type === 'cron' ? (
              <div style={{ display: 'grid', gridTemplateColumns: '2fr 1fr', gap: 8, marginTop: 8 }}>
                <div>
                  <div className="section-label">Cron expression (5-field)</div>
                  <input
                    type="text"
                    value={form.cron}
                    placeholder="0 2 * * *"
                    onChange={(e) => setForm({ ...form, cron: e.target.value })}
                  />
                </div>
                <div>
                  <div className="section-label">Timezone</div>
                  <input
                    type="text"
                    value={form.timezone}
                    placeholder="UTC"
                    onChange={(e) => setForm({ ...form, timezone: e.target.value })}
                  />
                </div>
              </div>
            ) : (
              <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 8, marginTop: 8 }}>
                <div>
                  <div className="section-label">Poll interval (seconds)</div>
                  <input
                    type="number"
                    value={form.poll_interval_seconds}
                    min={15}
                    onChange={(e) => setForm({ ...form, poll_interval_seconds: Number(e.target.value) })}
                  />
                  <div className="muted" style={{ fontSize: 10, marginTop: 2 }}>
                    How often we check DESCRIBE HISTORY.
                  </div>
                </div>
                <div>
                  <div className="section-label">Min interval between runs (seconds)</div>
                  <input
                    type="number"
                    value={form.min_interval_seconds}
                    min={60}
                    onChange={(e) => setForm({ ...form, min_interval_seconds: Number(e.target.value) })}
                  />
                  <div className="muted" style={{ fontSize: 10, marginTop: 2 }}>
                    Minimum time between consecutive fires.
                  </div>
                </div>
              </div>
            )}

            <div style={{ marginTop: 12, display: 'flex', alignItems: 'center', gap: 8 }}>
              <span className="section-label" style={{ margin: 0 }}>Enabled</span>
              <Toggle
                checked={form.enabled}
                onChange={(v) => setForm({ ...form, enabled: v })}
              />
            </div>

            {err && <div className="spike" style={{ marginTop: 8 }}>{err}</div>}

            <div style={{ display: 'flex', gap: 8, justifyContent: 'flex-end', marginTop: 16 }}>
              <button onClick={() => setModalOpen(false)}>Cancel</button>
              <button className="primary" disabled={saving} onClick={save}>
                {saving ? 'Saving…' : 'Save'}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
