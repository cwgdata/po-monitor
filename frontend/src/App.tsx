import { useEffect, useState } from 'react';
import { Selector } from './components/Selector';
import { TableCard } from './components/TableCard';
import { GroupCard } from './components/GroupCard';
import { AlertsPanel } from './components/AlertsPanel';
import { ConfigPanel } from './components/ConfigPanel';
import { FeedbackModal } from './components/FeedbackModal';
import { SchedulesPanel } from './components/SchedulesPanel';
import { WarehouseModal } from './components/WarehouseModal';
import { useSelection, groupRefKey } from './hooks/useSelection';
import { api, setActiveWarehouseId } from './lib/api';

type Tab = 'dashboard' | 'alerts' | 'schedules' | 'config';

export default function App() {
  const sel = useSelection();
  const [tab, setTab] = useState<Tab>('dashboard');
  const [user, setUser] = useState<{ email: string | null; name: string | null } | null>(null);
  const [autoRefreshSeconds, setAutoRefreshSeconds] = useState<number>(300);
  const [feedbackOpen, setFeedbackOpen] = useState(false);

  // Warehouse must be picked before any data routes can fire, since every
  // SQL request needs a warehouse_id. We load /api/config first; if no
  // warehouse is configured, show the modal and block the rest of the UI.
  // 'loading' = haven't checked yet, 'ready' = warehouse is set, 'missing' = show modal.
  const [whState, setWhState] = useState<'loading' | 'ready' | 'missing'>('loading');

  useEffect(() => {
    // whoami doesn't need a warehouse (it just reads headers), so it's safe
    // to run alongside the config check.
    api.whoami().then(setUser).catch(() => setUser(null));
    api.getConfig()
      .then((c) => {
        const v = c.thresholds?.auto_refresh_seconds;
        if (typeof v === 'number' && v >= 0) setAutoRefreshSeconds(v);
        const wh = c.warehouse_id_override || c.warehouse_id || '';
        if (wh) {
          setActiveWarehouseId(wh);
          setWhState('ready');
        } else {
          setWhState('missing');
        }
      })
      .catch(() => {
        // /api/config failure shouldn't trap the user — if the backend can't
        // tell us the warehouse, surface the picker so they can set one.
        setWhState('missing');
      });
  }, []);

  const onWarehousePicked = (id: string) => {
    setActiveWarehouseId(id);
    setWhState('ready');
  };

  const changeAutoRefresh = (seconds: number) => {
    setAutoRefreshSeconds(seconds);
    api.patchConfig({ thresholds: { auto_refresh_seconds: seconds } as any }).catch(() => {});
  };

  return (
    <div className="app">
      <aside className="sidebar">
        <h1 style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
          <svg viewBox="0 0 24 24" width="22" height="22" xmlns="http://www.w3.org/2000/svg" aria-hidden="true">
            <defs>
              <linearGradient id="logoGrad" x1="0" y1="0" x2="1" y2="1">
                <stop offset="0%" stopColor="#60a5fa" />
                <stop offset="100%" stopColor="#a78bfa" />
              </linearGradient>
            </defs>
            <rect x="2" y="4" width="20" height="4" rx="1.5" fill="url(#logoGrad)" opacity="0.40" />
            <rect x="2" y="10" width="20" height="4" rx="1.5" fill="url(#logoGrad)" opacity="0.70" />
            <rect x="2" y="16" width="20" height="4" rx="1.5" fill="url(#logoGrad)" />
            <path d="M16.5 12.5 l3 -3 l-1.8 0 l2.2 -3.5 l-3 3 l1.8 0 z" fill="#fbbf24" />
          </svg>
          <span>UC Managed Tables</span>
        </h1>
        <div className="subtitle">Predictive Optimization · Iceberg + Delta</div>

        {whState === 'ready' ? (
          <Selector
            catalog={sel.catalog}
            schema={sel.schema}
            tables={sel.tables}
            groups={sel.groups}
            onCatalog={sel.setCatalog}
            onSchema={sel.setSchema}
            onToggleTable={sel.toggleTable}
            onClear={sel.clearTables}
            onSetTables={sel.setTables}
            onToggleGroup={sel.toggleGroup}
            isGroupSelected={sel.isGroupSelected}
            autoRefreshSeconds={autoRefreshSeconds}
            onAutoRefreshChange={changeAutoRefresh}
            userEmail={user?.email || null}
          />
        ) : (
          <div className="muted" style={{ padding: 12, fontSize: 12 }}>
            {whState === 'loading' ? 'Loading…' : 'Pick a SQL warehouse to begin.'}
          </div>
        )}
      </aside>

      <main className="main">
        <div className="topbar">
          <div className="tabs">
            <button className={tab === 'dashboard' ? 'active' : ''} onClick={() => setTab('dashboard')}>Dashboard</button>
            <button className={tab === 'alerts' ? 'active' : ''} onClick={() => setTab('alerts')}>Alerts</button>
            <button className={tab === 'schedules' ? 'active' : ''} onClick={() => setTab('schedules')}>Schedules</button>
            <button className={tab === 'config' ? 'active' : ''} onClick={() => setTab('config')}>Config</button>
          </div>
          <div className="user-chip">
            <button className="logout-btn" onClick={() => setFeedbackOpen(true)} title="Send feedback">Feedback</button>
            {user?.email ? (
              <>
                <span className="user-avatar" aria-hidden>{(user.name || user.email).slice(0, 1).toUpperCase()}</span>
                <span className="user-email" title={user.email}>{user.email}</span>
                {/* Logout disabled — Databricks OIDC session cleanup is unreliable
                    across browser privacy modes. Use an incognito window for now
                    if you need a clean session. */}
              </>
            ) : (
              <a className="logout-btn" href="/" title="Sign in">Login</a>
            )}
          </div>
        </div>

        {/* All tab panels stay mounted; we toggle visibility via CSS so that
            switching tabs doesn't unmount TableCards (and lose their fetched
            data, spinners, running-op state). Each panel keeps its own polls
            running in the background — low cost, and data is already warm
            when the user comes back. */}
        {whState !== 'ready' ? (
          <div className="empty-state">
            {whState === 'loading'
              ? 'Loading…'
              : 'Pick a SQL warehouse to begin. Use the dialog at the right of the screen.'}
          </div>
        ) : (
        <>
        <div style={{ display: tab === 'dashboard' ? 'block' : 'none' }}>
          {sel.tables.length === 0 && sel.groups.length === 0 ? (
            <div className="empty-state">
              Select up to 20 managed tables (Iceberg or Delta) from the sidebar to begin monitoring.
              You can mix catalogs and schemas — switching the dropdowns won't clear your selection.
              <br /><br />
              Or click <strong>+ rollup</strong> next to a Catalog or Schema dropdown to add an
              aggregate health card spanning every managed table in that grouping.
            </div>
          ) : (
            <div className="grid">
              {sel.groups.map((g) => (
                <GroupCard
                  key={groupRefKey(g)}
                  groupRef={g}
                  onRemove={() => sel.toggleGroup(g)}
                  onAddTable={(t) => sel.toggleTable(t)}
                  autoRefreshSeconds={autoRefreshSeconds}
                />
              ))}
              {sel.tables.map((t) => (
                <TableCard
                  key={`${t.catalog}.${t.schema}.${t.table}`}
                  tableRef={t}
                  onRemove={() => sel.toggleTable(t)}
                  autoRefreshSeconds={autoRefreshSeconds}
                />
              ))}
            </div>
          )}
        </div>

        <div style={{ display: tab === 'alerts' ? 'block' : 'none' }}>
          <AlertsPanel />
        </div>
        <div style={{ display: tab === 'schedules' ? 'block' : 'none' }}>
          <SchedulesPanel userEmail={user?.email || null} />
        </div>
        <div style={{ display: tab === 'config' ? 'block' : 'none' }}>
          <ConfigPanel />
        </div>
        </>
        )}

        {feedbackOpen && (
          <FeedbackModal
            userEmail={user?.email || null}
            onClose={() => setFeedbackOpen(false)}
          />
        )}

        {whState === 'missing' && <WarehouseModal onSaved={onWarehousePicked} />}
      </main>
    </div>
  );
}
