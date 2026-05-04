import { useEffect, useRef, useState } from 'react';
import { LineChart, Line, BarChart, Bar, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts';
import { api, getActiveWarehouseId, type HealthResponse, type PoRun, type TableRef } from '../lib/api';
import { Toggle } from './Toggle';

// Inline SVG icons — simple, crisp at 14px, stroke uses currentColor so they
// inherit the button color scheme. Material/Feather conventions.
const IconRefresh = () => (
  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
    <path d="M21 12a9 9 0 1 1-3.5-7.1" />
    <polyline points="21 3 21 9 15 9" />
  </svg>
);
const IconExpand = () => (
  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
    <polyline points="15 3 21 3 21 9" />
    <polyline points="9 21 3 21 3 15" />
    <line x1="21" y1="3" x2="14" y2="10" />
    <line x1="3" y1="21" x2="10" y2="14" />
  </svg>
);
const IconRemove = () => (
  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
    <line x1="18" y1="6" x2="6" y2="18" />
    <line x1="6" y1="6" x2="18" y2="18" />
  </svg>
);

function TrendArrow({ pct }: { pct: number | null | undefined }) {
  if (pct == null) return null;
  if (Math.abs(pct) < 0.5) return <span className="trend-neutral trend-arrow">→ 0%</span>;
  const up = pct > 0;
  return (
    <span className={`trend-arrow ${up ? 'trend-up' : 'trend-down'}`}>
      {up ? '▲' : '▼'} {Math.abs(pct)}%
    </span>
  );
}

type Props = {
  tableRef: TableRef;
  onRemove: () => void;
  autoRefreshSeconds?: number;
};

function fmtBytes(n: number | undefined): string {
  if (!n && n !== 0) return '—';
  if (n > 1e12) return `${(n / 1e12).toFixed(2)} TB`;
  if (n > 1e9) return `${(n / 1e9).toFixed(2)} GB`;
  if (n > 1e6) return `${(n / 1e6).toFixed(2)} MB`;
  if (n > 1e3) return `${(n / 1e3).toFixed(1)} KB`;
  return `${n} B`;
}

function daysSince(iso?: string): string {
  if (!iso) return '—';
  const days = (Date.now() - new Date(iso).getTime()) / 86400000;
  if (days < 1) return `${Math.round(days * 24)}h`;
  return `${Math.round(days)}d`;
}

type Trends = {
  files_bytes: Array<{ date: string; files: number; bytes: number }>;
  dv_removed: Array<{ ts: string; dvs_removed: number; files_removed: number }>;
  size_history: Array<{ ts: string; bytes: number }>;
};
type Merges = { total: number; successful: number; failed: number; conflicts: number; conflict_rate: number; recent: any[]; error?: string };

const OPTIMIZE_OPS = new Set(['OPTIMIZE', 'COMPACTION', 'CLUSTERING']);

// localStorage-backed data cache, scoped per-table, so a page refresh or tab
// restore paints the last-known data instantly while the network fetch runs.
// Bump the version suffix whenever the cached shape changes.
const CACHE_VERSION = 'v1';
const cacheKey = (t: TableRef) =>
  `po-monitor-cache-${CACHE_VERSION}:${t.catalog}.${t.schema}.${t.table}`;

type CachedPayload = {
  ts: number;
  health: HealthResponse | null;
  runs: PoRun[];
  trends: Trends | null;
  merges: Merges | null;
};

function loadCache(t: TableRef): CachedPayload | null {
  try {
    const raw = localStorage.getItem(cacheKey(t));
    if (!raw) return null;
    const parsed = JSON.parse(raw) as CachedPayload;
    if (!parsed || typeof parsed.ts !== 'number') return null;
    return parsed;
  } catch { return null; }
}

function saveCache(t: TableRef, payload: Omit<CachedPayload, 'ts'>) {
  try {
    localStorage.setItem(cacheKey(t), JSON.stringify({ ts: Date.now(), ...payload }));
  } catch { /* quota exceeded or disabled — non-fatal */ }
}

export function TableCard({ tableRef, onRemove, autoRefreshSeconds = 60 }: Props) {
  // Initialize from localStorage (fast-path on same device).
  const initial = loadCache(tableRef);
  const [health, setHealth] = useState<HealthResponse | null>(initial?.health ?? null);
  const [runs, setRuns] = useState<PoRun[]>(initial?.runs ?? []);
  const [trends, setTrends] = useState<Trends | null>(initial?.trends ?? null);
  const [merges, setMerges] = useState<Merges | null>(initial?.merges ?? null);
  const [cachedAt, setCachedAt] = useState<number | null>(initial?.ts ?? null);

  // If localStorage had nothing, try hydrating from the server-side UC cache.
  // This kicks in on a fresh browser / device for a returning user.
  useEffect(() => {
    if (initial) return; // localStorage win — skip server hydrate
    let cancelled = false;
    api.getCardCache(tableRef.catalog, tableRef.schema, tableRef.table)
      .then((r) => {
        if (cancelled || !r.payload) return;
        const p = r.payload as { health?: HealthResponse; runs?: PoRun[]; trends?: Trends; merges?: Merges };
        if (p.health) setHealth(p.health);
        if (p.runs) setRuns(p.runs);
        if (p.trends) setTrends(p.trends);
        if (p.merges) setMerges(p.merges);
        const ts = r.updated_at ? new Date(r.updated_at).getTime() : Date.now();
        setCachedAt(ts);
        // Also warm localStorage so next page refresh is instant.
        saveCache(tableRef, { health: p.health ?? null, runs: p.runs ?? [], trends: p.trends ?? null, merges: p.merges ?? null });
      })
      .catch(() => { /* server cache miss — fine, the live fetch is in flight */ });
    return () => { cancelled = true; };
  }, [tableRef.catalog, tableRef.schema, tableRef.table]);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [toast, setToast] = useState<string | null>(null);
  const [refreshing, setRefreshing] = useState(false);
  const [expanded, setExpanded] = useState(false);
  const [running, setRunning] = useState<Array<{ operation_type: string; executed_by: string; status: string; start_time: string }>>([]);

  const [warehouseBlocked, setWarehouseBlocked] = useState<string | null>(null);
  const [confirm, setConfirm] = useState<null | { key: string; label: string; target: string; details?: string; danger?: boolean; run: () => Promise<any> }>(null);
  type OpStatus = 'launching' | 'running';
  const [opStatus, setOpStatus] = useState<Record<string, OpStatus>>({});
  const [hasSeenRunning, setHasSeenRunning] = useState<Record<string, boolean>>({});
  const [opSubmittedAt, setOpSubmittedAt] = useState<Record<string, string>>({});

  // fetchGen + mounted guard against stale resolves: the user removes the
  // card mid-fetch, the parent swaps tableRef, or auto-refresh races a
  // manual refresh. Each refresh bumps the gen; only the latest commits.
  const fetchGen = useRef(0);
  const mounted = useRef(true);
  useEffect(() => {
    mounted.current = true;
    return () => { mounted.current = false; };
  }, []);

  const refresh = async () => {
    const gen = ++fetchGen.current;
    const isStale = () => !mounted.current || gen !== fetchGen.current;

    // Guard: don't auto-start a stopped warehouse. Check state via the
    // management API (no-start) before firing queries.
    const whId = getActiveWarehouseId();
    if (whId) {
      try {
        const { warehouses } = await api.listWarehouses();
        if (isStale()) return;
        const wh = warehouses.find((w) => w.id === whId);
        const state = (wh?.state || '').toUpperCase();
        if (wh && state !== 'RUNNING' && state !== 'STARTING') {
          setWarehouseBlocked(`${wh.name} is ${state}. Start the warehouse to load data.`);
          return;
        }
      } catch {
        // If the status check itself fails, fall through and let queries error naturally.
      }
    }
    if (isStale()) return;
    setWarehouseBlocked(null);
    setRefreshing(true);
    Promise.all([
      api.getHealth(tableRef.catalog, tableRef.schema, tableRef.table).catch((e) => {
        if (!isStale()) setErr(e.message);
        return null;
      }),
      api.getPoRuns(tableRef.catalog, tableRef.schema, tableRef.table, 30).catch(() => ({ runs: [] })),
      api.getTrends(tableRef.catalog, tableRef.schema, tableRef.table, 30).catch(() => null),
      api.getMerges(tableRef.catalog, tableRef.schema, tableRef.table, 24).catch(() => null),
    ]).then(([h, r, t, m]) => {
      if (isStale()) return;
      const newRuns = r?.runs || [];
      const newTrends = t || null;
      const newMerges = m || null;
      if (h) setHealth(h);
      setRuns(newRuns);
      setTrends(newTrends);
      setMerges(newMerges);
      // Persist to cache only when we got a real health response —
      // otherwise we'd nuke good cached data on a transient failure.
      if (h) {
        const payload = { health: h, runs: newRuns, trends: newTrends, merges: newMerges };
        saveCache(tableRef, payload);
        setCachedAt(Date.now());
        // Fire-and-forget the server-side cache save. If it fails, no user impact.
        api.saveCardCache({ catalog: tableRef.catalog, schema: tableRef.schema, table: tableRef.table, payload })
          .catch(() => { /* non-fatal */ });
      }
    }).finally(() => {
      if (!isStale()) setRefreshing(false);
    });
  };

  useEffect(() => { refresh(); }, [tableRef.catalog, tableRef.schema, tableRef.table]);

  useEffect(() => {
    if (!autoRefreshSeconds || autoRefreshSeconds <= 0) return;
    const interval = Math.max(15, autoRefreshSeconds) * 1000;
    const id = setInterval(refresh, interval);
    return () => clearInterval(id);
  }, [tableRef.catalog, tableRef.schema, tableRef.table, autoRefreshSeconds]);

  // Running-ops poll. Expensive (scans system.query.history with an ILIKE),
  // so only run it at a fast cadence while a user-kicked op is in-flight.
  // Otherwise, a slow background check is plenty — or none at all.
  const hasPendingOp = Object.keys(opStatus).length > 0;
  useEffect(() => {
    let cancelled = false;
    const poll = () => {
      api.getRunning(tableRef.catalog, tableRef.schema, tableRef.table)
        .then((r) => { if (!cancelled) setRunning(r.running || []); })
        .catch(() => { if (!cancelled) setRunning([]); });
    };
    // Initial hit so the pill shows anything already running at load time.
    poll();
    // Fast (5s) while a button is launching/running; slow (5 min) otherwise.
    const interval = hasPendingOp ? 5_000 : 300_000;
    const id = setInterval(poll, interval);
    return () => { cancelled = true; clearInterval(id); };
  }, [tableRef.catalog, tableRef.schema, tableRef.table, hasPendingOp]);

  // Lifecycle-aware action runner: launching → running → idle.
  // The confirm modal closes immediately; the button reflects current state.
  const runConfirmed = () => {
    if (!confirm) return;
    const { key, run } = confirm;
    setErr(null); setToast(null);
    setConfirm(null);
    const submittedAt = new Date().toISOString();
    setOpStatus((s) => ({ ...s, [key]: 'launching' }));
    setHasSeenRunning((h) => ({ ...h, [key]: false }));
    setOpSubmittedAt((m) => ({ ...m, [key]: submittedAt }));
    (async () => {
      let submitResp: any = null;
      try {
        submitResp = await run();
      } catch (e: any) {
        setErr(e.message);
        setOpStatus((s) => { const n = { ...s }; delete n[key]; return n; });
        return;
      }
      // Op already completed inside the 5s wait window — clear fast.
      if (submitResp && (submitResp.done === true || submitResp.status === 'done')) {
        setOpStatus((s) => { const n = { ...s }; delete n[key]; return n; });
        refresh();
        return;
      }
      // Poll both running-queries AND the ops list (DESCRIBE HISTORY).
      // Some ops finish in the gap between submit and first poll — catching
      // them in the runs list flips launching → idle cleanly.
      let ticks = 0;
      const MAX_TICKS = 20; // 20 * 3s = 60s
      const matchesRun = (r: PoRun) => {
        const ts = r.start_time || '';
        if (ts <= submittedAt) return false;
        if (key === 'OPTIMIZE') return ['OPTIMIZE', 'COMPACTION', 'CLUSTERING'].includes(r.operation_type);
        if (key === 'VACUUM') return r.operation_type === 'VACUUM';
        return false;
      };
      const poll = async () => {
        let completedInRuns = false;
        try {
          const [runningR, runsR] = await Promise.all([
            api.getRunning(tableRef.catalog, tableRef.schema, tableRef.table),
            api.getPoRuns(tableRef.catalog, tableRef.schema, tableRef.table, 2),
          ]);
          setRunning(runningR.running || []);
          const newRuns = runsR.runs || [];
          if (newRuns.some(matchesRun)) {
            completedInRuns = true;
            setRuns(newRuns);
          }
        } catch { /* noop */ }
        if (completedInRuns) {
          setOpStatus((s) => { const n = { ...s }; delete n[key]; return n; });
          refresh();
          return;
        }
        ticks += 1;
        if (ticks >= MAX_TICKS) {
          setOpStatus((s) => {
            if (s[key] !== 'launching') return s;
            const n = { ...s }; delete n[key]; return n;
          });
          refresh();
          return;
        }
        setTimeout(poll, 3000);
      };
      // First poll fast to catch sub-second ops
      setTimeout(poll, 500);
    })();
  };

  const askConfirm = (key: string, label: string, target: string, run: () => Promise<any>, opts?: { details?: string; danger?: boolean }) => {
    setConfirm({ key, label, target, run, details: opts?.details, danger: opts?.danger });
  };

  // Match running operations to our op keys; flip launching → running;
  // once an op that was running is no longer running, clear status + refresh.
  useEffect(() => {
    const matches = (key: string) => {
      if (key === 'OPTIMIZE') return running.some((r) => ['OPTIMIZE', 'COMPACTION', 'CLUSTERING'].includes(r.operation_type));
      if (key === 'VACUUM') return running.some((r) => r.operation_type === 'VACUUM');
      return false;
    };
    const transitions: Record<string, OpStatus | null> = {};
    const newlySeen: Record<string, boolean> = {};
    Object.entries(opStatus).forEach(([key, status]) => {
      const isRunning = matches(key);
      if (status === 'launching' && isRunning) {
        transitions[key] = 'running';
        newlySeen[key] = true;
      } else if (status === 'running' && !isRunning && hasSeenRunning[key]) {
        transitions[key] = null;
      }
    });
    if (Object.keys(transitions).length === 0 && Object.keys(newlySeen).length === 0) return;
    if (Object.keys(transitions).length > 0) {
      setOpStatus((s) => {
        const next = { ...s };
        for (const [k, v] of Object.entries(transitions)) {
          if (v === null) delete next[k];
          else next[k] = v;
        }
        return next;
      });
    }
    if (Object.keys(newlySeen).length > 0) {
      setHasSeenRunning((h) => ({ ...h, ...newlySeen }));
    }
    // If we just cleared any, trigger a data refresh
    if (Object.values(transitions).includes(null)) {
      refresh();
    }
  }, [running]);

  const fullName = `${tableRef.catalog}.${tableRef.schema}.${tableRef.table}`;
  const badge = health?.badge || 'unknown';

  // -------- Reusable JSX blocks --------

  const kpiTiles = (
    <>
      <div className="tile-row">
        <div className="kpi">
          <div className="label">Num files <span className="muted" style={{ fontSize: 9 }}>(7d)</span></div>
          <div className="value">
            {health?.detail?.num_files?.toLocaleString() ?? '—'}
            <TrendArrow pct={health?.trend?.files_pct} />
          </div>
        </div>
        <div className="kpi">
          <div className="label">Avg file size <span className="muted" style={{ fontSize: 9 }}>(7d)</span></div>
          <div className="value">
            {fmtBytes(health?.detail?.avg_file_size_bytes)}
            <TrendArrow pct={health?.trend?.avg_size_pct} />
          </div>
        </div>
        <div className="kpi">
          <div className="label">Total size <span className="muted" style={{ fontSize: 9 }}>(7d)</span></div>
          <div className="value">
            {fmtBytes(health?.detail?.size_bytes)}
            <TrendArrow pct={health?.trend?.bytes_pct} />
          </div>
        </div>
      </div>

      <div className="tile-row">
        <div className="kpi">
          <div className="label">DV count</div>
          <div className="value">{health?.detail?.dv_count?.toLocaleString() ?? '—'}</div>
          {health?.detail?.rows_deleted_by_dv != null && (
            <div className="muted" style={{ fontSize: 10 }}>
              {health.detail.rows_deleted_by_dv.toLocaleString()} rows marked deleted
            </div>
          )}
        </div>
        <div className="kpi">
          <div className="label">Unclustered (proxy)</div>
          <div className="value">
            {health?.unclustered_proxy?.files_since_last_optimize?.toLocaleString() ?? '—'}
          </div>
          <div className="muted" style={{ fontSize: 10 }}>
            {fmtBytes(health?.unclustered_proxy?.bytes_since_last_optimize ?? undefined)} added since last OPTIMIZE
          </div>
        </div>
        <div className="kpi">
          <div className="label">MERGE 24h</div>
          <div className="value">
            {merges?.total ?? '—'}
            {merges && merges.conflicts > 0 && (
              <span className="trend-up trend-arrow">⚠ {merges.conflicts} conflict{merges.conflicts === 1 ? '' : 's'}</span>
            )}
          </div>
          <div className="muted" style={{ fontSize: 10 }}>
            {merges ? `${merges.successful} ok · ${merges.failed} failed` : 'loading…'}
          </div>
        </div>
      </div>

      <div className="tile-row">
        <div className="kpi">
          <div className="label">Since OPTIMIZE</div>
          <div className="value">{daysSince(health?.last_optimize?.start_time)}</div>
        </div>
        <div className="kpi">
          <div className="label">Since VACUUM</div>
          <div className="value">{daysSince(health?.last_vacuum?.start_time)}</div>
        </div>
      </div>
    </>
  );

  // Prefer commit-by-commit size (from DESCRIBE HISTORY) — works even when
  // the daily-snapshot system table is empty. Fall back to daily snapshots.
  const sizeSeries: Array<{ label: string; gb: number }> = (() => {
    if (trends?.size_history && trends.size_history.length > 1) {
      return trends.size_history.map((r) => ({
        label: (r.ts || '').slice(5, 16).replace('T', ' '),
        gb: r.bytes / 1e9,
      }));
    }
    if (trends?.files_bytes && trends.files_bytes.length > 1) {
      return trends.files_bytes.map((r) => ({ label: r.date.slice(5), gb: r.bytes / 1e9 }));
    }
    return [];
  })();

  const sizeChart = sizeSeries.length > 1 ? (
    <div style={{ height: 90, marginBottom: 8 }}>
      <div className="label" style={{ fontSize: 11, color: 'var(--muted)', marginBottom: 4 }}>Table size over time ({sizeSeries.length} commits)</div>
      <ResponsiveContainer>
        <LineChart data={sizeSeries} margin={{ top: 6, right: 12, left: 44, bottom: 22 }}>
          <XAxis dataKey="label" tick={{ fill: 'var(--muted)', fontSize: 10 }} tickLine={{ stroke: 'var(--border)' }} axisLine={{ stroke: 'var(--border)' }}
                 label={{ value: 'Commit time', position: 'insideBottom', offset: -8, fill: 'var(--muted)', fontSize: 11 }} />
          <YAxis tick={{ fill: 'var(--muted)', fontSize: 10 }} tickLine={{ stroke: 'var(--border)' }} axisLine={{ stroke: 'var(--border)' }}
                 tickFormatter={(v: number) => v >= 1000 ? `${(v / 1000).toFixed(1)} TB` : `${v.toFixed(1)} GB`}
                 label={{ value: 'Size', angle: -90, position: 'insideLeft', offset: 4, fill: 'var(--muted)', fontSize: 11 }} />
          <Tooltip formatter={(v: number) => fmtBytes(v * 1e9)} contentStyle={{ background: 'var(--panel-2)', border: '1px solid var(--border)', fontSize: 12 }} />
          <Line type="monotone" dataKey="gb" stroke="#a78bfa" strokeWidth={2} dot={{ r: 1 }} />
        </LineChart>
      </ResponsiveContainer>
    </div>
  ) : null;

  const filesCompactedChart = (() => {
    const data = runs
      .filter((r) => OPTIMIZE_OPS.has(r.operation_type) && r.files_compacted != null)
      .slice(0, 30)
      .reverse()
      .map((r) => ({ ts: r.start_time?.slice(5, 10), files: r.files_compacted || 0 }));
    if (data.length === 0) return null;
    return (
      <div style={{ height: 110, marginBottom: 8 }}>
        <div className="label" style={{ fontSize: 11, color: 'var(--muted)', marginBottom: 4 }}>Files compacted per OPTIMIZE / COMPACTION run</div>
        <ResponsiveContainer>
          <LineChart data={data} margin={{ top: 6, right: 12, left: 36, bottom: 22 }}>
            <XAxis dataKey="ts" tick={{ fill: 'var(--muted)', fontSize: 10 }} tickLine={{ stroke: 'var(--border)' }} axisLine={{ stroke: 'var(--border)' }}
                   label={{ value: 'Date (MM-DD)', position: 'insideBottom', offset: -8, fill: 'var(--muted)', fontSize: 11 }} />
            <YAxis tick={{ fill: 'var(--muted)', fontSize: 10 }} tickLine={{ stroke: 'var(--border)' }} axisLine={{ stroke: 'var(--border)' }}
                   tickFormatter={(v: number) => v >= 1000 ? `${(v / 1000).toFixed(1)}k` : String(v)}
                   label={{ value: 'Files', angle: -90, position: 'insideLeft', offset: 4, fill: 'var(--muted)', fontSize: 11 }} />
            <Tooltip contentStyle={{ background: 'var(--panel-2)', border: '1px solid var(--border)', fontSize: 12 }} />
            <Line type="monotone" dataKey="files" stroke="var(--accent)" strokeWidth={2} dot={{ r: 2 }} />
          </LineChart>
        </ResponsiveContainer>
      </div>
    );
  })();

  const dailyFilesChart = trends && trends.files_bytes.length > 1 ? (
    <div style={{ height: 90, marginBottom: 8 }}>
      <div className="label" style={{ fontSize: 11, color: 'var(--muted)', marginBottom: 4 }}>Daily file count (30d)</div>
      <ResponsiveContainer>
        <LineChart data={trends.files_bytes.map((r) => ({ date: r.date.slice(5), files: r.files }))}
                   margin={{ top: 6, right: 12, left: 36, bottom: 22 }}>
          <XAxis dataKey="date" tick={{ fill: 'var(--muted)', fontSize: 10 }} tickLine={{ stroke: 'var(--border)' }} axisLine={{ stroke: 'var(--border)' }}
                 label={{ value: 'Date (MM-DD)', position: 'insideBottom', offset: -8, fill: 'var(--muted)', fontSize: 11 }} />
          <YAxis tick={{ fill: 'var(--muted)', fontSize: 10 }} tickLine={{ stroke: 'var(--border)' }} axisLine={{ stroke: 'var(--border)' }}
                 tickFormatter={(v: number) => v >= 1000 ? `${(v / 1000).toFixed(1)}k` : String(v)}
                 label={{ value: 'Active files', angle: -90, position: 'insideLeft', offset: 4, fill: 'var(--muted)', fontSize: 11 }} />
          <Tooltip contentStyle={{ background: 'var(--panel-2)', border: '1px solid var(--border)', fontSize: 12 }} />
          <Line type="monotone" dataKey="files" stroke="#60a5fa" strokeWidth={2} dot={{ r: 2 }} />
        </LineChart>
      </ResponsiveContainer>
    </div>
  ) : null;

  const dvsRemovedChart = trends && trends.dv_removed.length > 0 ? (
    <div style={{ height: 90, marginBottom: 8 }}>
      <div className="label" style={{ fontSize: 11, color: 'var(--muted)', marginBottom: 4 }}>DVs removed per OPTIMIZE run</div>
      <ResponsiveContainer>
        <BarChart data={trends.dv_removed.map((r) => ({ ts: (r.ts || '').slice(5, 10), dvs: r.dvs_removed }))}
                  margin={{ top: 6, right: 12, left: 36, bottom: 22 }}>
          <XAxis dataKey="ts" tick={{ fill: 'var(--muted)', fontSize: 10 }} tickLine={{ stroke: 'var(--border)' }} axisLine={{ stroke: 'var(--border)' }}
                 label={{ value: 'Date (MM-DD)', position: 'insideBottom', offset: -8, fill: 'var(--muted)', fontSize: 11 }} />
          <YAxis tick={{ fill: 'var(--muted)', fontSize: 10 }} tickLine={{ stroke: 'var(--border)' }} axisLine={{ stroke: 'var(--border)' }}
                 label={{ value: 'DVs removed', angle: -90, position: 'insideLeft', offset: 4, fill: 'var(--muted)', fontSize: 11 }} />
          <Tooltip contentStyle={{ background: 'var(--panel-2)', border: '1px solid var(--border)', fontSize: 12 }} />
          <Bar dataKey="dvs" fill="#a78bfa" />
        </BarChart>
      </ResponsiveContainer>
    </div>
  ) : null;

  const renderOpsRow = (r: PoRun, i: number) => {
    const isCompact = OPTIMIZE_OPS.has(r.operation_type);
    let sizeCell: React.ReactNode = '—';
    if (isCompact && (r.bytes_compacted != null || r.bytes_output != null)) {
      const removed = (r.bytes_compacted ?? 0) - (r.bytes_output ?? 0);
      const pct = r.bytes_compacted ? (removed / r.bytes_compacted * 100) : null;
      const cls = removed > 0 ? 'trend-down' : removed < 0 ? 'trend-up' : 'trend-neutral';
      sizeCell = (
        <span className={cls} title={pct != null ? `${removed >= 0 ? '' : '+'}${(-pct).toFixed(1)}% vs pre-compaction` : ''}>
          {removed >= 0 ? '' : '+'}{fmtBytes(Math.abs(removed))}
        </span>
      );
    } else if (r.operation_type === 'VACUUM' && r.bytes_deleted) {
      sizeCell = <span className="trend-down" title="reclaimed by VACUUM">{fmtBytes(r.bytes_deleted)}</span>;
    }
    return (
      <tr key={`${r.source}-${r.start_time}-${i}`}>
        <td><span className="badge" style={{ fontSize: 10 }}>{r.source === 'po' ? 'PO' : 'USER'}</span></td>
        <td>{r.operation_type}</td>
        <td><span className={`badge ${r.operation_status === 'SUCCESSFUL' ? 'green' : r.operation_status === 'FAILED' ? 'red' : 'amber'}`}>{r.operation_status}</span></td>
        <td className="muted">{daysSince(r.start_time)} ago</td>
        <td>{r.batch_count && r.batch_count > 1 ? r.batch_count : '—'}</td>
        <td>{r.files_compacted?.toLocaleString() ?? '—'}</td>
        <td style={{ maxWidth: 200 }}>{sizeCell}</td>
      </tr>
    );
  };

  const opsTable = (limit?: number) => (
    runs.length === 0 ? (
      <div className="muted" style={{ padding: 8 }}>No runs in the last 30 days.</div>
    ) : (
      <div className="table-scroll">
        <table className="data">
          <thead><tr><th>Source</th><th>Type</th><th>Status</th><th>When</th><th>Batches</th><th>Files</th><th>Bytes Removed</th></tr></thead>
          <tbody>
            {(limit ? runs.slice(0, limit) : runs).map(renderOpsRow)}
          </tbody>
        </table>
      </div>
    )
  );

  const statusLabel = (key: string, idle: string): string => {
    const s = opStatus[key];
    if (s === 'launching') return 'Launching…';
    if (s === 'running') return 'Running…';
    return idle;
  };
  const isBusy = (key: string) => !!opStatus[key];
  const anyBusy = Object.keys(opStatus).length > 0;

  const actionRow = (
    <div className="action-row">
      <button
        className="btn primary"
        disabled={isBusy('OPTIMIZE')}
        onClick={() => askConfirm('OPTIMIZE', 'OPTIMIZE', fullName,
          () => api.runOptimize(tableRef),
          { details: 'Runs OPTIMIZE on the table using the selected warehouse.' })}>
        {statusLabel('OPTIMIZE', 'Run OPTIMIZE')}
      </button>
      <button
        className="btn"
        disabled={isBusy('VACUUM')}
        onClick={() => askConfirm('VACUUM', 'VACUUM LITE', fullName,
          () => api.runVacuum({ ...tableRef, mode: 'LITE' }),
          { details: 'Removes soft-deleted files still within retention. Safe to run frequently.' })}>
        {statusLabel('VACUUM', 'VACUUM LITE')}
      </button>
      <button
        className="btn danger"
        disabled={isBusy('VACUUM')}
        onClick={() => askConfirm('VACUUM', 'VACUUM FULL', fullName,
          () => api.runVacuum({ ...tableRef, mode: 'FULL' }),
          { details: 'Permanently removes unreferenced files. Cannot be undone.', danger: true })}>
        {opStatus['VACUUM'] ? statusLabel('VACUUM', '') : 'VACUUM FULL'}
      </button>
      {(() => {
        const poOn = health?.po_state?.enabled === true;
        const poKnown = health?.po_state?.enabled != null;
        const nextState = !poOn;
        const busyText = opStatus['PO_TOGGLE'] ? statusLabel('PO_TOGGLE', '') : null;
        return (
          <span
            className="btn"
            style={{ display: 'inline-flex', alignItems: 'center', gap: 6, cursor: 'default', padding: '6px 12px' }}
            title={health?.po_state?.raw ? `Current: ${health.po_state.raw}` : ''}
          >
            <span className="muted" style={{ fontSize: 11 }}>PO</span>
            <Toggle
              checked={poOn}
              disabled={!poKnown || isBusy('PO_TOGGLE')}
              label={busyText || undefined}
              onChange={() => askConfirm('PO_TOGGLE',
                nextState ? 'Enable PO' : 'Disable PO',
                fullName,
                async () => {
                  const r = await api.togglePO({ ...tableRef, enabled: nextState });
                  setTimeout(() => {
                    setOpStatus((s) => { const n = { ...s }; delete n['PO_TOGGLE']; return n; });
                    refresh();
                  }, 1500);
                  return r;
                },
                { details: `Current: ${health?.po_state?.raw || '?'}` },
              )}
            />
          </span>
        );
      })()}
      <button
        className="btn"
        disabled={isBusy('FORCE_PO')}
        onClick={() => askConfirm('FORCE_PO', 'Force PO', fullName,
          async () => {
            const r = await api.forceTrigger(tableRef).catch((e) => { throw e; });
            setTimeout(() => setOpStatus((s) => { const n = { ...s }; delete n['FORCE_PO']; return n; }), 1500);
            return r;
          },
          { details: 'Submits OPTIMIZE + VACUUM LITE as a stand-in for the PO scheduler.' })}>
        {statusLabel('FORCE_PO', 'Force PO')}
      </button>
      <button
        className="btn"
        disabled={isBusy('SCHEDULE')}
        onClick={() => {
          const cron = window.prompt('Cron for OPTIMIZE (e.g. 0 2 * * *):', '0 2 * * *');
          if (!cron) return;
          askConfirm('SCHEDULE', 'Schedule OPTIMIZE', fullName,
            async () => {
              const r = await api.schedule({ ...tableRef, operation: 'OPTIMIZE', cron, timezone_id: 'UTC' }).catch((e) => { throw e; });
              setTimeout(() => setOpStatus((s) => { const n = { ...s }; delete n['SCHEDULE']; return n; }), 1500);
              return r;
            },
            { details: `cron: ${cron}` },
          );
        }}>
        {statusLabel('SCHEDULE', 'Schedule')}
      </button>
    </div>
  );
  void anyBusy;

  return (
    <div className="card">
      <h3 style={{ flexWrap: 'wrap', gap: 8 }}>
        <span style={{ wordBreak: 'break-all', flex: '1 1 auto', minWidth: 0, fontFamily: 'var(--mono, ui-monospace, monospace)', display: 'flex', alignItems: 'center', gap: 8 }} title={fullName}>
          <span>{fullName}</span>
          {refreshing && <span className="spinner-inline" aria-label="refreshing" />}
          {!refreshing && cachedAt && (
            <span
              className="muted"
              style={{ fontSize: 10, fontFamily: 'system-ui, sans-serif', fontWeight: 400 }}
              title={`Last update: ${new Date(cachedAt).toLocaleString()}`}
            >
              {(() => {
                const ageMin = (Date.now() - cachedAt) / 60000;
                if (ageMin < 1) return 'just now';
                if (ageMin < 60) return `${Math.round(ageMin)}m ago`;
                const ageHr = ageMin / 60;
                if (ageHr < 24) return `${Math.round(ageHr)}h ago`;
                return `${Math.round(ageHr / 24)}d ago`;
              })()}
            </span>
          )}
        </span>
        <span style={{ display: 'flex', gap: 8, alignItems: 'center', flexShrink: 0 }}>
          {running.length > 0 && (
            <span className="running-pill" title={running.map(r => `${r.operation_type} by ${r.executed_by?.split('@')[0] || '?'} — ${r.status}`).join('\n')}>
              <span className="running-pulse" />
              {running[0].operation_type}{running.length > 1 ? ` +${running.length - 1}` : ''}
            </span>
          )}
          <span className={`badge ${badge}`}>{badge}</span>
          <button
            className={`btn icon-btn ${refreshing ? 'spinning' : ''}`}
            onClick={refresh}
            disabled={refreshing}
            aria-label="Refresh"
            title="Refresh this card"
          ><IconRefresh /></button>
          <button
            className="btn icon-btn"
            onClick={() => setExpanded(true)}
            aria-label="Expand"
            title="Expand"
          ><IconExpand /></button>
          <button
            className="btn icon-btn"
            onClick={onRemove}
            aria-label="Remove"
            title="Remove from dashboard"
          ><IconRemove /></button>
        </span>
      </h3>

      {warehouseBlocked && (
        <div className="spike" style={{ marginBottom: 8 }}>
          ⚠ {warehouseBlocked}
        </div>
      )}

      {health?.reasons && health.reasons.length > 0 && (
        <div className="muted" style={{ marginBottom: 8 }}>{health.reasons.join(' · ')}</div>
      )}

      {health?.spike && <div className="spike">SPIKE: {health.spike}</div>}

      {kpiTiles}

      {sizeChart}

      <div className="label" style={{ fontSize: 11, color: 'var(--muted)', marginBottom: 4 }}>Recent operations (PO + manual)</div>
      {opsTable(5)}

      {actionRow}

      {toast && <div className="stub" style={{ marginTop: 8 }}>{toast}</div>}
      {err && <div className="spike" style={{ marginTop: 8 }}>Error: {err}</div>}

      {confirm && (
        <div className="modal-backdrop" onClick={(e) => { if (e.target === e.currentTarget) setConfirm(null); }}>
          <div className="card" style={{ width: 460, maxWidth: '90vw', margin: 0 }}>
            <h3 style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
              <span>Confirm action</span>
              <button className="btn" onClick={() => setConfirm(null)}>Cancel</button>
            </h3>
            <div className="section-label">Action</div>
            <div className="value" style={{ marginBottom: 10 }}>{confirm.label}</div>
            <div className="section-label">Target</div>
            <div className="muted" style={{ fontFamily: 'var(--mono, ui-monospace, monospace)', wordBreak: 'break-all', marginBottom: 10 }}>{confirm.target}</div>
            {confirm.details && (
              <>
                <div className="section-label">Details</div>
                <div className="muted" style={{ fontSize: 12, marginBottom: 12 }}>{confirm.details}</div>
              </>
            )}
            <div className="action-row" style={{ justifyContent: 'flex-end' }}>
              <button className="btn" onClick={() => setConfirm(null)}>Cancel</button>
              <button className={`btn ${confirm.danger ? 'danger' : 'primary'}`} onClick={runConfirmed}>
                {confirm.danger ? 'Run anyway' : 'Confirm'}
              </button>
            </div>
          </div>
        </div>
      )}

      {expanded && (
        <div className="modal-backdrop" onClick={(e) => { if (e.target === e.currentTarget) setExpanded(false); }}>
          <div className="card" style={{ width: '90vw', maxWidth: 1100, maxHeight: '90vh', overflowY: 'auto', margin: 0 }}>
            <h3 style={{ flexWrap: 'wrap', gap: 8 }}>
              <span style={{ wordBreak: 'break-all', flex: '1 1 auto', minWidth: 0, fontFamily: 'var(--mono, ui-monospace, monospace)' }}>{fullName}</span>
              <span style={{ display: 'flex', gap: 8, alignItems: 'center', flexShrink: 0 }}>
                {running.length > 0 && (
                  <span className="running-pill">
                    <span className="running-pulse" />
                    {running[0].operation_type}{running.length > 1 ? ` +${running.length - 1}` : ''}
                  </span>
                )}
                <span className={`badge ${badge}`}>{badge}</span>
                <button className="btn" onClick={() => setExpanded(false)}>Close</button>
              </span>
            </h3>

            {health?.reasons && health.reasons.length > 0 && (
              <div className="muted" style={{ marginBottom: 8 }}>{health.reasons.join(' · ')}</div>
            )}
            {health?.spike && <div className="spike">SPIKE: {health.spike}</div>}

            {kpiTiles}

            {sizeChart}
            {dailyFilesChart}
            {filesCompactedChart}
            {dvsRemovedChart}

            <div className="label" style={{ fontSize: 11, color: 'var(--muted)', marginBottom: 4, marginTop: 16 }}>
              All operations in last 30 days ({runs.length} rows)
            </div>
            {runs.length === 0 ? (
              <div className="muted" style={{ padding: 8 }}>No runs.</div>
            ) : (
              <div className="table-scroll" style={{ maxHeight: '50vh' }}>
                <table className="data">
                  <thead>
                    <tr>
                      <th>Source</th><th>Type</th><th>Status</th><th>When</th><th>Batches</th>
                      <th>Files in</th><th>Files out</th><th>Bytes in</th><th>Bytes out</th><th>Duration</th><th>DBUs</th>
                    </tr>
                  </thead>
                  <tbody>
                    {runs.map((r, i) => (
                      <tr key={`${r.source}-${r.start_time}-${i}`}>
                        <td><span className="badge" style={{ fontSize: 10 }}>{r.source === 'po' ? 'PO' : 'USER'}</span></td>
                        <td>{r.operation_type}</td>
                        <td><span className={`badge ${r.operation_status === 'SUCCESSFUL' ? 'green' : r.operation_status === 'FAILED' ? 'red' : 'amber'}`}>{r.operation_status}</span></td>
                        <td className="muted" title={r.start_time}>{daysSince(r.start_time)} ago</td>
                        <td>{r.batch_count && r.batch_count > 1 ? r.batch_count : '—'}</td>
                        <td>{r.files_compacted?.toLocaleString() ?? '—'}</td>
                        <td>{r.files_output?.toLocaleString() ?? '—'}</td>
                        <td>{r.bytes_compacted ? fmtBytes(r.bytes_compacted) : '—'}</td>
                        <td>{r.bytes_output ? fmtBytes(r.bytes_output) : '—'}</td>
                        <td>{r.duration_seconds != null ? `${r.duration_seconds}s` : '—'}</td>
                        <td>{r.dbus != null ? r.dbus.toFixed(2) : '—'}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}

            {merges && merges.recent && merges.recent.length > 0 && (
              <>
                <div className="label" style={{ fontSize: 11, color: 'var(--muted)', marginBottom: 4, marginTop: 16 }}>
                  Recent MERGE queries (24h) — {merges.total} total · {merges.successful} ok · {merges.failed} failed · {merges.conflicts} conflicts
                </div>
                <div className="table-scroll">
                  <table className="data">
                    <thead>
                      <tr><th>Status</th><th>When</th><th>Duration</th><th>Error</th></tr>
                    </thead>
                    <tbody>
                      {merges.recent.map((m: any, i: number) => (
                        <tr key={i}>
                          <td><span className={`badge ${m.status === 'FINISHED' ? 'green' : 'red'}`}>{m.status}</span></td>
                          <td className="muted" title={m.start_time}>{daysSince(m.start_time)} ago</td>
                          <td>{m.duration_ms != null ? `${Math.round(m.duration_ms / 1000)}s` : '—'}</td>
                          <td className="muted" style={{ fontSize: 11, maxWidth: 600 }} title={m.error || ''}>{m.error || '—'}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </>
            )}

            <div style={{ marginTop: 16 }}>
              {actionRow}
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
