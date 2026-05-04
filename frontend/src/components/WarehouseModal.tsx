import { useEffect, useState } from 'react';
import { api, setActiveWarehouseId } from '../lib/api';

type Warehouse = { id: string; name: string; state?: string; size?: string };

type Props = {
  /** Called once a warehouse is successfully selected and saved. */
  onSaved: (warehouseId: string) => void;
};

/**
 * First-run / no-warehouse modal. Blocks the dashboard until the user picks
 * a SQL warehouse — all data routes need one to issue the X-Warehouse-Id
 * header, and the very first /api/config write itself runs SQL so we have
 * to know the warehouse before any persistence happens.
 *
 * Save flow:
 *   1. setActiveWarehouseId(id) — header is sent on the very next request
 *   2. patchConfig({ warehouse_id_override: id }) — persists across sessions
 *   3. onSaved(id) — parent unblocks the rest of the UI
 */
export function WarehouseModal({ onSaved }: Props) {
  const [warehouses, setWarehouses] = useState<Warehouse[]>([]);
  const [picked, setPicked] = useState<string>('');
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    api.listWarehouses()
      .then((r) => {
        setWarehouses(r.warehouses);
        // Pre-select the first running warehouse if one exists, else the first
        // entry overall — the user can override before clicking Save.
        const running = r.warehouses.find((w) => (w.state || '').toUpperCase() === 'RUNNING');
        if (running) setPicked(running.id);
        else if (r.warehouses.length > 0) setPicked(r.warehouses[0].id);
      })
      .catch((e) => setErr(`Couldn't load warehouses: ${e.message || e}`))
      .finally(() => setLoading(false));
  }, []);

  const save = async () => {
    if (!picked) return;
    setSaving(true);
    setErr(null);
    setActiveWarehouseId(picked);
    try {
      await api.patchConfig({ warehouse_id_override: picked });
      onSaved(picked);
    } catch (e: any) {
      // Patch failed — most likely the app SP can't write to the configured
      // catalog. Roll back the active id so the next attempt starts clean.
      setActiveWarehouseId(null);
      setErr(`Save failed: ${e.message || e}`);
      setSaving(false);
    }
  };

  const dotColor = (state?: string): string => {
    const s = (state || '').toUpperCase();
    if (s === 'RUNNING' || s === 'STARTED') return '#4ade80';
    if (s === 'STOPPED' || s === 'DELETED') return '#f87171';
    if (s === 'STARTING' || s === 'STOPPING') return '#fbbf24';
    return 'var(--muted)';
  };

  // Backdrop has no onClick — modal is intentionally non-dismissible.
  return (
    <div className="modal-backdrop">
      <div className="card" style={{ width: 480, maxWidth: '90vw', margin: 0 }}>
        <h3 style={{ marginTop: 0 }}>Pick a SQL warehouse</h3>
        <div className="muted" style={{ fontSize: 12, marginBottom: 16 }}>
          PO Monitor needs a warehouse to query Unity Catalog and the PO
          system tables. You can change it any time from the sidebar.
        </div>

        {loading && <div className="muted" style={{ padding: 8 }}>Loading warehouses…</div>}

        {!loading && warehouses.length === 0 && (
          <div className="spike">
            No SQL warehouses are visible to you in this workspace. Ask an admin
            to grant <code>CAN_USE</code> on at least one warehouse.
          </div>
        )}

        {!loading && warehouses.length > 0 && (
          <ul className="table-list" style={{ marginTop: 0, marginBottom: 12 }}>
            {warehouses.map((w) => (
              <li
                key={w.id}
                className={picked === w.id ? 'selected' : ''}
                onClick={() => setPicked(w.id)}
                style={{ cursor: 'pointer' }}
              >
                <input
                  type="radio"
                  name="wh-pick"
                  checked={picked === w.id}
                  onChange={() => setPicked(w.id)}
                  style={{ marginRight: 8 }}
                />
                <span
                  aria-hidden
                  style={{
                    display: 'inline-block',
                    width: 8, height: 8, borderRadius: '50%',
                    background: dotColor(w.state),
                    marginRight: 8,
                  }}
                />
                <span style={{ flex: 1 }}>{w.name}</span>
                <span className="muted" style={{ fontSize: 10 }}>{w.state || 'UNKNOWN'}</span>
              </li>
            ))}
          </ul>
        )}

        {err && <div className="spike" style={{ marginTop: 8 }}>{err}</div>}

        <div style={{ display: 'flex', gap: 8, marginTop: 12 }}>
          <button
            className="primary"
            disabled={!picked || saving || loading}
            onClick={save}
            style={{ flex: 1 }}
          >
            {saving ? 'Saving…' : 'Save'}
          </button>
        </div>
      </div>
    </div>
  );
}
