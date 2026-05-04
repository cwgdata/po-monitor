import { useEffect, useState } from 'react';
import { api, setActiveWarehouseId, type TableRef, type GroupRef, type SavedDashboard } from '../lib/api';

type Props = {
  catalog: string | null;
  schema: string | null;
  tables: TableRef[];
  groups: GroupRef[];
  onCatalog: (c: string | null) => void;
  onSchema: (s: string | null) => void;
  onToggleTable: (t: TableRef) => void;
  onClear: () => void;
  onSetTables: (ts: TableRef[]) => void;
  onToggleGroup: (g: GroupRef) => void;
  isGroupSelected: (g: GroupRef) => boolean;
  autoRefreshSeconds: number;
  onAutoRefreshChange: (seconds: number) => void;
  userEmail: string | null;
};

const REFRESH_OPTIONS: Array<{ label: string; value: number }> = [
  { label: 'OFF', value: 0 },
  { label: '1 min', value: 60 },
  { label: '5 min', value: 300 },
  { label: '30 min', value: 1800 },
  { label: '60 min', value: 3600 },
];

export function Selector({
  catalog, schema, tables, groups,
  onCatalog, onSchema, onToggleTable, onClear, onSetTables,
  onToggleGroup, isGroupSelected,
  autoRefreshSeconds, onAutoRefreshChange, userEmail,
}: Props) {
  const [catalogs, setCatalogs] = useState<string[]>([]);
  const [schemas, setSchemas] = useState<string[]>([]);
  const [tbls, setTbls] = useState<Array<{ table_name: string; data_source_format: string }>>([]);
  const [managedOnly, setManagedOnly] = useState(true);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  // Warehouse selector
  const [warehouses, setWarehouses] = useState<Array<{ id: string; name: string; state?: string; size?: string }>>([]);
  const [activeWarehouse, setActiveWarehouse] = useState<string>('');
  const [whSaving, setWhSaving] = useState(false);

  // Saved dashboards
  const [configs, setConfigs] = useState<SavedDashboard[]>([]);
  const [saveOpen, setSaveOpen] = useState(false);
  const [saveName, setSaveName] = useState('');
  const [saveBusy, setSaveBusy] = useState(false);

  const refreshWarehouses = () => {
    api.listWarehouses().then((r) => setWarehouses(r.warehouses)).catch(() => {});
  };

  const refreshConfigs = () => {
    api.listDashboards().then((r) => setConfigs(r.configs)).catch(() => {});
  };

  // Warehouse status polls every 15s, independent of the dashboard auto-refresh
  // setting — we always want the green/red dot to be current.
  useEffect(() => {
    refreshWarehouses();
    api.getConfig().then((c) => {
      const wh = c.warehouse_id_override || c.warehouse_id || '';
      setActiveWarehouse(wh);
      setActiveWarehouseId(wh || null); // ensure every request carries the header
    }).catch(() => {});
    const id = setInterval(refreshWarehouses, 15_000);
    return () => clearInterval(id);
  }, []);

  useEffect(() => {
    refreshConfigs();
  }, [userEmail]);

  const changeWarehouse = async (id: string) => {
    setWhSaving(true);
    setActiveWarehouseId(id || null); // route subsequent requests to this warehouse
    try {
      await api.patchConfig({ warehouse_id_override: id || null });
      setActiveWarehouse(id);
      // Refresh catalog list — the new warehouse may see a different catalog set
      api.listCatalogs().then((r) => setCatalogs(r.catalogs)).catch(() => {});
    } catch (e: any) {
      setErr(`Warehouse save failed: ${e.message}`);
    } finally {
      setWhSaving(false);
    }
  };

  useEffect(() => {
    setLoading(true);
    api.listCatalogs()
      .then((r) => setCatalogs(r.catalogs))
      .catch((e) => setErr(e.message))
      .finally(() => setLoading(false));
  }, []);

  useEffect(() => {
    if (!catalog) { setSchemas([]); return; }
    setLoading(true);
    api.listSchemas(catalog)
      .then((r) => setSchemas(r.schemas))
      .catch((e) => setErr(e.message))
      .finally(() => setLoading(false));
  }, [catalog]);

  useEffect(() => {
    if (!catalog || !schema) { setTbls([]); return; }
    setLoading(true);
    api.listTables(catalog, schema, managedOnly)
      .then((r) => setTbls(r.tables))
      .catch((e) => setErr(e.message))
      .finally(() => setLoading(false));
  }, [catalog, schema, managedOnly]);

  const isSelected = (name: string) =>
    tables.some((t) => t.catalog === catalog && t.schema === schema && t.table === name);

  const activeWh = warehouses.find((w) => w.id === activeWarehouse);
  const dotColor = (state?: string): string => {
    const s = (state || '').toUpperCase();
    if (s === 'RUNNING' || s === 'STARTED') return '#4ade80'; // green
    if (s === 'STOPPED' || s === 'DELETED') return '#f87171'; // red
    if (s === 'STARTING' || s === 'STOPPING') return '#fbbf24'; // amber
    return 'var(--muted)';
  };

  // Count unique (catalog, schema) pairs so the user sees when they've got a
  // cross-schema dashboard loaded — a tiny trust-builder for the new feature.
  const uniquePairs = new Set(tables.map((t) => `${t.catalog}.${t.schema}`));

  const saveDashboard = async () => {
    const name = saveName.trim();
    if (!name) return;
    setSaveBusy(true);
    try {
      await api.saveDashboard({ name, tables });
      setSaveOpen(false);
      setSaveName('');
      refreshConfigs();
    } catch (e: any) {
      setErr(`Save failed: ${e.message}`);
    } finally {
      setSaveBusy(false);
    }
  };

  const loadDashboard = async (config_id: string) => {
    try {
      const cfg = await api.loadDashboard(config_id);
      onSetTables(cfg.tables || []);
      // Also snap the catalog/schema to the first loaded table so the user
      // can see where to pick up browsing. Not required; just convenient.
      if (cfg.tables && cfg.tables.length > 0) {
        onCatalog(cfg.tables[0].catalog);
        onSchema(cfg.tables[0].schema);
      }
    } catch (e: any) {
      setErr(`Load failed: ${e.message}`);
    }
  };

  const deleteDashboard = async (config_id: string, name: string) => {
    if (!confirm(`Delete dashboard "${name}"?`)) return;
    try {
      await api.deleteDashboard(config_id);
      refreshConfigs();
    } catch (e: any) {
      setErr(`Delete failed: ${e.message}`);
    }
  };

  return (
    <div>
      <div className="section-label">Auto-refresh</div>
      <select
        value={String(autoRefreshSeconds)}
        onChange={(e) => onAutoRefreshChange(Number(e.target.value))}
      >
        {REFRESH_OPTIONS.map((o) => (
          <option key={o.value} value={String(o.value)}>{o.label}</option>
        ))}
      </select>

      <div className="section-label" style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
        <span>SQL Warehouse</span>
        {activeWh && (
          <span
            title={`${activeWh.name} · ${activeWh.state || 'UNKNOWN'}`}
            style={{
              display: 'inline-block',
              width: 8,
              height: 8,
              borderRadius: '50%',
              background: dotColor(activeWh.state),
            }}
          />
        )}
      </div>
      <select
        value={activeWarehouse}
        disabled={whSaving}
        onChange={(e) => changeWarehouse(e.target.value)}
      >
        <option value="">(none)</option>
        {warehouses.map((w) => (
          <option key={w.id} value={w.id}>
            {w.name}
          </option>
        ))}
      </select>
      {whSaving && <div className="muted" style={{ fontSize: 11, marginTop: 4 }}>Saving…</div>}

      {/* Saved dashboards — per-user */}
      <div className="section-label" style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
        <span>Saved dashboards</span>
        <button
          onClick={() => { setSaveName(''); setSaveOpen(true); }}
          disabled={!userEmail || tables.length === 0}
          title={
            !userEmail ? 'Sign in to save' :
            tables.length === 0 ? 'Select tables first' : 'Save current selection'
          }
          style={{ padding: '2px 8px', fontSize: 11 }}
        >
          Save…
        </button>
      </div>
      {!userEmail ? (
        <div className="muted" style={{ padding: 8, fontSize: 11 }}>Sign in to save dashboards.</div>
      ) : configs.length === 0 ? (
        <div className="muted" style={{ padding: 8, fontSize: 11 }}>No saved dashboards yet.</div>
      ) : (
        <ul className="table-list" style={{ marginTop: 4 }}>
          {configs.map((c) => (
            <li key={c.config_id} style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
              <span
                style={{ flex: 1, cursor: 'pointer' }}
                onClick={() => loadDashboard(c.config_id)}
                title={`Load ${c.name} (${c.tables.length} tables)`}
              >
                {c.name}
                <span className="muted" style={{ marginLeft: 6, fontSize: 10 }}>
                  {c.tables.length}t
                </span>
              </span>
              <button
                onClick={(e) => { e.stopPropagation(); deleteDashboard(c.config_id, c.name); }}
                style={{ padding: '0 6px', fontSize: 11, lineHeight: '20px' }}
                title="Delete dashboard"
              >×</button>
            </li>
          ))}
        </ul>
      )}

      {saveOpen && (
        <div style={{
          background: 'var(--panel-2)',
          border: '1px solid var(--border)',
          borderRadius: 6,
          padding: 10,
          marginTop: 6,
        }}>
          <div className="section-label" style={{ marginTop: 0 }}>Dashboard name</div>
          <input
            autoFocus
            type="text"
            value={saveName}
            placeholder="e.g. weekly-hotspots"
            onChange={(e) => setSaveName(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter') saveDashboard();
              else if (e.key === 'Escape') setSaveOpen(false);
            }}
          />
          <div style={{ display: 'flex', gap: 6, marginTop: 6 }}>
            <button
              className="primary"
              disabled={saveBusy || !saveName.trim()}
              onClick={saveDashboard}
              style={{ flex: 1 }}
            >
              {saveBusy ? 'Saving…' : 'Save'}
            </button>
            <button onClick={() => setSaveOpen(false)} style={{ flex: 1 }}>Cancel</button>
          </div>
        </div>
      )}

      <div className="section-label" style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
        <span>Catalog</span>
        {catalog && (
          <button
            onClick={() => onToggleGroup({ kind: 'catalog', catalog })}
            disabled={!catalog}
            title={isGroupSelected({ kind: 'catalog', catalog })
              ? 'Remove this catalog rollup card'
              : 'Add a catalog-level rollup card to the dashboard'}
            style={{ padding: '2px 8px', fontSize: 11 }}
          >
            {isGroupSelected({ kind: 'catalog', catalog }) ? '− rollup' : '+ rollup'}
          </button>
        )}
      </div>
      <select value={catalog || ''} onChange={(e) => onCatalog(e.target.value || null)}>
        <option value="">Select catalog…</option>
        {catalogs.map((c) => <option key={c} value={c}>{c}</option>)}
      </select>

      <div className="section-label" style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
        <span>Schema</span>
        {catalog && schema && (
          <button
            onClick={() => onToggleGroup({ kind: 'schema', catalog, schema })}
            title={isGroupSelected({ kind: 'schema', catalog, schema })
              ? 'Remove this schema rollup card'
              : 'Add a schema-level rollup card to the dashboard'}
            style={{ padding: '2px 8px', fontSize: 11 }}
          >
            {isGroupSelected({ kind: 'schema', catalog, schema }) ? '− rollup' : '+ rollup'}
          </button>
        )}
      </div>
      <select value={schema || ''} disabled={!catalog} onChange={(e) => onSchema(e.target.value || null)}>
        <option value="">Select schema…</option>
        {schemas.map((s) => <option key={s} value={s}>{s}</option>)}
      </select>

      {groups.length > 0 && (
        <>
          <div className="section-label">Rollup cards ({groups.length})</div>
          <ul className="table-list">
            {groups.map((g) => {
              const k = g.kind === 'schema' ? `${g.catalog}.${g.schema}` : g.catalog;
              return (
                <li
                  key={`${g.kind}:${k}`}
                  className="selected"
                  onClick={() => onToggleGroup(g)}
                  title="Remove this rollup"
                >
                  <span style={{
                    fontSize: 9,
                    padding: '1px 4px',
                    borderRadius: 3,
                    background: 'var(--panel-2)',
                    color: 'var(--muted)',
                    fontWeight: 600,
                    letterSpacing: 0.4,
                  }}>{g.kind === 'schema' ? 'S' : 'C'}</span>
                  <span style={{ flex: 1, fontFamily: 'var(--mono, ui-monospace, monospace)', fontSize: 11 }}>{k}</span>
                </li>
              );
            })}
          </ul>
        </>
      )}

      <div className="section-label" style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
        <span>Tables ({tables.length}/20{uniquePairs.size > 1 ? ` · ${uniquePairs.size} schemas` : ''})</span>
        <label style={{ fontSize: 11, textTransform: 'none', letterSpacing: 'normal', display: 'flex', gap: 4, alignItems: 'center' }}>
          <input type="checkbox" checked={managedOnly} onChange={(e) => setManagedOnly(e.target.checked)} />
          managed only
        </label>
      </div>

      {!catalog || !schema ? (
        <div className="muted" style={{ padding: 8 }}>Select a catalog and schema.</div>
      ) : loading ? (
        <div className="muted" style={{ padding: 8 }}>Loading…</div>
      ) : tbls.length === 0 ? (
        <div className="muted" style={{ padding: 8 }}>No {managedOnly ? 'managed' : ''} tables found.</div>
      ) : (
        <ul className="table-list">
          {tbls.map((t) => {
            const ref: TableRef = { catalog: catalog!, schema: schema!, table: t.table_name };
            return (
              <li key={t.table_name} className={isSelected(t.table_name) ? 'selected' : ''} onClick={() => onToggleTable(ref)}>
                <input type="checkbox" checked={isSelected(t.table_name)} readOnly />
                <span style={{ flex: 1 }}>{t.table_name}</span>
                <span className="muted" style={{ fontSize: 10 }}>{t.data_source_format}</span>
              </li>
            );
          })}
        </ul>
      )}

      {tables.length > 0 && (
        <button onClick={onClear} style={{ width: '100%', marginTop: 8 }}>Clear selection</button>
      )}
      {err && <div className="spike" style={{ marginTop: 8 }}>{err}</div>}
    </div>
  );
}
