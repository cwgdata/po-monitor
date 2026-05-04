import { useEffect, useRef, useState } from 'react';
import { api, type GroupHealthResponse, type GroupRef, type TableRef } from '../lib/api';
import { Toggle } from './Toggle';

function fmtBytes(n: number | undefined): string {
  if (!n && n !== 0) return '—';
  if (n > 1e12) return `${(n / 1e12).toFixed(2)} TB`;
  if (n > 1e9) return `${(n / 1e9).toFixed(2)} GB`;
  if (n > 1e6) return `${(n / 1e6).toFixed(2)} MB`;
  if (n > 1e3) return `${(n / 1e3).toFixed(1)} KB`;
  return `${n} B`;
}

function daysSince(iso?: string | null): string {
  if (!iso) return '—';
  const t = new Date(iso).getTime();
  if (!t) return '—';
  const days = (Date.now() - t) / 86400000;
  if (days < 1) return `${Math.round(days * 24)}h`;
  return `${Math.round(days)}d`;
}

function fmtNum(n: number | undefined): string {
  if (!n && n !== 0) return '—';
  return n.toLocaleString();
}

function IconRefresh() {
  return (
    <svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.6">
      <path d="M13.5 8a5.5 5.5 0 0 1-9.4 3.9" /><path d="M2.5 8A5.5 5.5 0 0 1 11.9 4.1" />
      <path d="M14 1v4h-4" /><path d="M2 15v-4h4" />
    </svg>
  );
}

function IconRemove() {
  return (
    <svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.6">
      <path d="M3 4h10" /><path d="M5 4l1-2h4l1 2" />
      <path d="M5 4l1 9h4l1-9" />
    </svg>
  );
}

type Props = {
  groupRef: GroupRef;
  onRemove: () => void;
  onAddTable?: (t: TableRef) => void;
  autoRefreshSeconds: number;
  maxTables?: number;
};

export function GroupCard({ groupRef, onRemove, onAddTable, autoRefreshSeconds, maxTables = 50 }: Props) {
  const [health, setHealth] = useState<GroupHealthResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [loadedAt, setLoadedAt] = useState<number | null>(null);

  // fetchGen tracks the current request generation; mounted flips false on
  // unmount. Together they discard stale fetches (groupRef changed mid-flight,
  // auto-refresh raced a manual refresh, or component unmounted before resolve).
  const fetchGen = useRef(0);
  const mounted = useRef(true);
  useEffect(() => {
    mounted.current = true;
    return () => { mounted.current = false; };
  }, []);

  const refresh = async () => {
    const gen = ++fetchGen.current;
    setLoading(true);
    setErr(null);
    try {
      const r = await api.getGroupHealth(groupRef.catalog, groupRef.schema, maxTables);
      if (!mounted.current || gen !== fetchGen.current) return;
      setHealth(r);
      setLoadedAt(Date.now());
    } catch (e: any) {
      if (!mounted.current || gen !== fetchGen.current) return;
      setErr(e?.message || 'failed to load');
    } finally {
      if (mounted.current && gen === fetchGen.current) setLoading(false);
    }
  };

  useEffect(() => { refresh(); /* eslint-disable-next-line */ }, [groupRef.catalog, groupRef.schema, groupRef.kind]);

  useEffect(() => {
    if (!autoRefreshSeconds) return;
    const id = setInterval(refresh, autoRefreshSeconds * 1000);
    return () => clearInterval(id);
    /* eslint-disable-next-line */
  }, [autoRefreshSeconds, groupRef.catalog, groupRef.schema, groupRef.kind]);

  const title = groupRef.kind === 'schema'
    ? `${groupRef.catalog}.${groupRef.schema}`
    : groupRef.catalog;
  const kindLabel = groupRef.kind === 'schema' ? 'schema rollup' : 'catalog rollup';
  const badge = health?.badge || (loading ? 'unknown' : 'unknown');

  const counts = health?.counts;
  const totalEval = health?.evaluated_tables ?? 0;
  const totalAll = health?.total_tables ?? 0;
  const truncated = health?.truncated;

  const poState = health?.po_state;
  const [poBusy, setPoBusy] = useState(false);

  const togglePO = async (next: boolean) => {
    setPoBusy(true);
    try {
      await api.togglePOGroup({
        kind: groupRef.kind,
        catalog: groupRef.catalog,
        schema: groupRef.schema,
        enabled: next,
      });
      // Refresh so we pick up the new po_state from DESCRIBE.
      refresh();
    } catch (e: any) {
      setErr(`Toggle failed: ${e.message || e}`);
    } finally {
      setPoBusy(false);
    }
  };

  return (
    <div className="card">
      <h3 style={{ flexWrap: 'wrap', gap: 8 }}>
        <span style={{ wordBreak: 'break-all', flex: '1 1 auto', minWidth: 0, display: 'flex', alignItems: 'center', gap: 8 }} title={title}>
          <span
            aria-hidden
            style={{
              fontSize: 10,
              padding: '1px 6px',
              borderRadius: 4,
              background: 'var(--panel-2, #1a1a22)',
              color: 'var(--muted, #aaa)',
              fontFamily: 'system-ui, sans-serif',
              fontWeight: 600,
              letterSpacing: 0.4,
              textTransform: 'uppercase',
              flexShrink: 0,
            }}
          >
            {groupRef.kind === 'schema' ? 'SCHEMA' : 'CATALOG'}
          </span>
          <span style={{ fontFamily: 'var(--mono, ui-monospace, monospace)' }}>{title}</span>
          {loadedAt && !loading && (
            <span
              className="muted"
              style={{ fontSize: 10, fontFamily: 'system-ui, sans-serif', fontWeight: 400 }}
              title={`Last refresh: ${new Date(loadedAt).toLocaleString()}`}
            >
              {(() => {
                const m = (Date.now() - loadedAt) / 60000;
                if (m < 1) return 'just now';
                if (m < 60) return `${Math.round(m)}m ago`;
                return `${Math.round(m / 60)}h ago`;
              })()}
            </span>
          )}
        </span>
        <span style={{ display: 'flex', gap: 8, alignItems: 'center', flexShrink: 0 }}>
          <span className={`badge ${badge}`}>{badge}</span>
          <button
            className={`btn icon-btn ${loading ? 'spinning' : ''}`}
            onClick={refresh}
            disabled={loading}
            aria-label="Refresh"
            title="Refresh this rollup"
          ><IconRefresh /></button>
          <button
            className="btn icon-btn"
            onClick={onRemove}
            aria-label="Remove"
            title="Remove from dashboard"
          ><IconRemove /></button>
        </span>
      </h3>

      <div className="muted" style={{ fontSize: 11, marginBottom: 8 }}>
        {kindLabel}
        {totalAll > 0 && <> · {totalEval}/{totalAll} managed tables evaluated</>}
        {truncated && <> · capped at {maxTables}</>}
      </div>

      {err && <div className="spike">Error: {err}</div>}

      {/* PO toggle for the catalog/schema. Setting it here flips the default
          for every contained table that doesn't have its own override. */}
      {health && (
        <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 8, fontSize: 11 }}>
          <span className="muted" style={{ textTransform: 'uppercase', letterSpacing: 0.04 }}>
            Predictive Optimization
          </span>
          <Toggle
            checked={poState?.enabled === true}
            disabled={poBusy || poState?.enabled === null}
            onChange={togglePO}
            title={
              poState?.raw
                ? `Current: ${poState.raw}${poState.inherited ? ' (inherited)' : ''}`
                : `Toggle PO at the ${groupRef.kind} level`
            }
          />
          {poState?.inherited && (
            <span className="muted" style={{ fontSize: 10 }}>(inherited)</span>
          )}
          {poState?.enabled === null && !err && (
            <span className="muted" style={{ fontSize: 10 }}>state unknown</span>
          )}
        </div>
      )}

      {/* Counts breakdown */}
      <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', marginBottom: 12 }}>
        <CountTile label="red" value={counts?.red ?? 0} colorClass="red" />
        <CountTile label="amber" value={counts?.amber ?? 0} colorClass="amber" />
        <CountTile label="green" value={counts?.green ?? 0} colorClass="green" />
        {(counts?.error ?? 0) > 0 && (
          <CountTile label="error" value={counts!.error} colorClass="muted" />
        )}
      </div>

      {/* Aggregate KPIs */}
      <div className="tile-row">
        <KpiTile label="Total size" value={fmtBytes(health?.totals?.size_bytes)} />
        <KpiTile label="Total files" value={fmtNum(health?.totals?.num_files)} />
        <KpiTile label="Avg file" value={fmtBytes(health?.totals?.avg_file_size_bytes)} />
      </div>
      <div className="tile-row">
        <KpiTile label="Avg fail rate" value={health ? `${(health.avg_failure_rate * 100).toFixed(0)}%` : '—'} />
        <KpiTile label="Last OPTIMIZE" value={daysSince(health?.last_optimize_max)} />
        <KpiTile label="Last VACUUM" value={daysSince(health?.last_vacuum_max)} />
      </div>

      {/* Top offenders */}
      {health && health.offenders.length > 0 && (
        <div style={{ marginTop: 12 }}>
          <div className="label" style={{ fontSize: 11, color: 'var(--muted)', marginBottom: 4 }}>
            Top offenders ({health.offenders.length})
          </div>
          <table className="data" style={{ width: '100%' }}>
            <thead>
              <tr>
                <th style={{ width: 60 }}>Badge</th>
                <th>Table</th>
                <th>Reasons</th>
                {onAddTable && <th style={{ width: 36 }}></th>}
              </tr>
            </thead>
            <tbody>
              {health.offenders.map((o) => (
                <tr key={`${o.catalog}.${o.schema}.${o.table}`}>
                  <td><span className={`badge ${o.badge}`}>{o.badge}</span></td>
                  <td style={{ fontFamily: 'var(--mono, ui-monospace, monospace)', fontSize: 11 }}>
                    {groupRef.kind === 'catalog' ? `${o.schema}.${o.table}` : o.table}
                  </td>
                  <td style={{ fontSize: 11 }}>
                    {(o.reasons || []).join(' · ') || '—'}
                  </td>
                  {onAddTable && (
                    <td style={{ textAlign: 'right' }}>
                      <button
                        className="btn"
                        title="Open this table as its own card"
                        style={{ padding: '0 6px', fontSize: 11, lineHeight: '20px' }}
                        onClick={() => onAddTable({ catalog: o.catalog, schema: o.schema, table: o.table })}
                      >+</button>
                    </td>
                  )}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {health && health.offenders.length === 0 && totalEval > 0 && (
        <div className="muted" style={{ fontSize: 12, marginTop: 8 }}>
          No red/amber tables. Everything in this {groupRef.kind} is healthy.
        </div>
      )}
    </div>
  );
}

function CountTile({ label, value, colorClass }: { label: string; value: number; colorClass: string }) {
  return (
    <div className="kpi" style={{ minWidth: 80, textAlign: 'center' }}>
      <div className={`badge ${colorClass}`} style={{ fontSize: 10, marginBottom: 4 }}>{label}</div>
      <div style={{ fontSize: 22, fontWeight: 600 }}>{value}</div>
    </div>
  );
}

function KpiTile({ label, value }: { label: string; value: string }) {
  return (
    <div className="kpi">
      <div className="label">{label}</div>
      <div className="value">{value}</div>
    </div>
  );
}
