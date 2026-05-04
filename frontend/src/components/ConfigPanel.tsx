import { useEffect, useState } from 'react';
import { api, type AppConfig } from '../lib/api';

type WarehouseOpt = { id: string; name: string; state?: string; size?: string };

export function ConfigPanel() {
  const [cfg, setCfg] = useState<AppConfig | null>(null);
  const [patch, setPatch] = useState<Partial<AppConfig>>({});
  const [thrPatch, setThrPatch] = useState<Record<string, number>>({});
  const [catalogs, setCatalogs] = useState<string[]>([]);
  const [schemas, setSchemas] = useState<string[]>([]);
  const [warehouses, setWarehouses] = useState<WarehouseOpt[]>([]);
  const [msg, setMsg] = useState<{ kind: 'ok' | 'err'; text: string } | null>(null);
  const [saving, setSaving] = useState(false);

  // Initial load: config + catalog list + warehouse list (best-effort)
  useEffect(() => {
    api.getConfig().then(setCfg);
    api.listCatalogs().then((r) => setCatalogs(r.catalogs || [])).catch(() => setCatalogs([]));
    api.listWarehouses().then((r) => setWarehouses(r.warehouses || [])).catch(() => setWarehouses([]));
  }, []);

  // Cascade: when default_catalog changes (either in cfg or patch), refresh schemas
  const effectiveCatalog = patch.default_catalog ?? cfg?.default_catalog ?? '';
  useEffect(() => {
    if (effectiveCatalog) {
      api.listSchemas(effectiveCatalog).then((r) => setSchemas(r.schemas || [])).catch(() => setSchemas([]));
    } else {
      setSchemas([]);
    }
  }, [effectiveCatalog]);

  const dirty = Object.keys(patch).length > 0 || Object.keys(thrPatch).length > 0;

  const save = async () => {
    setSaving(true);
    try {
      const payload: any = { ...patch };
      if (Object.keys(thrPatch).length) payload.thresholds = thrPatch;
      const updated = await api.patchConfig(payload);
      setCfg(updated as AppConfig);
      setPatch({});
      setThrPatch({});
      setMsg({ kind: 'ok', text: 'Saved.' });
      setTimeout(() => setMsg(null), 2000);
    } catch (e: any) {
      setMsg({ kind: 'err', text: e?.message || 'Save failed' });
    } finally {
      setSaving(false);
    }
  };

  if (!cfg) return <div className="empty-state">Loading config…</div>;

  const thrVal = (k: string): number => {
    if (k in thrPatch) return thrPatch[k];
    return cfg.thresholds[k];
  };
  const setThr = (k: string, v: number) => setThrPatch((p) => ({ ...p, [k]: v }));

  const maskedWebhook = (v?: string | null) =>
    v ? v.replace(/^(https?:\/\/[^/]+\/services\/).*$/, '$1••••••') : '';

  return (
    <div className="grid" style={{ gridTemplateColumns: '1fr 1fr' }}>
      <div className="card">
        <h3>Workspace</h3>

        <div className="section-label">Workspace host</div>
        <input type="text" readOnly value={cfg.workspace_host || ''} />
        <div className="muted" style={{ marginTop: 4 }}>
          Auto-detected from Databricks App environment.
        </div>

        <div className="section-label">SQL Warehouse</div>
        {warehouses.length > 0 ? (
          <select
            value={patch.warehouse_id_override ?? cfg.warehouse_id_override ?? cfg.warehouse_id ?? ''}
            onChange={(e) => setPatch({ ...patch, warehouse_id_override: e.target.value })}
          >
            <option value="">(use resource binding: {cfg.warehouse_id || 'none'})</option>
            {warehouses.map((w) => (
              <option key={w.id} value={w.id}>
                {w.name} — {w.id} {w.state ? `(${w.state})` : ''}
              </option>
            ))}
          </select>
        ) : (
          <input
            type="text"
            value={patch.warehouse_id_override ?? cfg.warehouse_id_override ?? cfg.warehouse_id ?? ''}
            onChange={(e) => setPatch({ ...patch, warehouse_id_override: e.target.value })}
            placeholder="warehouse id"
          />
        )}
        <div className="muted" style={{ marginTop: 4 }}>
          Default comes from the <code>sql-warehouse</code> resource binding; override here if you want a different warehouse.
        </div>

        <div className="section-label">Mode</div>
        <input
          type="text"
          readOnly
          value={cfg.is_databricks_app ? 'Running inside Databricks App' : 'Local dev'}
        />

        <div className="section-label">Default catalog</div>
        <select
          value={patch.default_catalog ?? cfg.default_catalog ?? ''}
          onChange={(e) => setPatch({ ...patch, default_catalog: e.target.value, default_schema: '' })}
        >
          <option value="">(none)</option>
          {catalogs.map((c) => (
            <option key={c} value={c}>{c}</option>
          ))}
        </select>

        <div className="section-label">Default schema</div>
        <select
          value={patch.default_schema ?? cfg.default_schema ?? ''}
          onChange={(e) => setPatch({ ...patch, default_schema: e.target.value })}
          disabled={!effectiveCatalog}
        >
          <option value="">(none)</option>
          {schemas.map((s) => (
            <option key={s} value={s}>{s}</option>
          ))}
        </select>
      </div>

      <div className="card">
        <h3>Alerts</h3>

        <div className="section-label">Slack webhook URL</div>
        <input
          type="url"
          value={patch.slack_webhook_url ?? cfg.slack_webhook_url ?? ''}
          onChange={(e) => setPatch({ ...patch, slack_webhook_url: e.target.value })}
          placeholder={cfg.slack_webhook_url ? maskedWebhook(cfg.slack_webhook_url) : 'https://hooks.slack.com/services/…'}
        />

        <div className="section-label">Alert email</div>
        <input
          type="text"
          value={patch.alert_email_to ?? cfg.alert_email_to ?? ''}
          onChange={(e) => setPatch({ ...patch, alert_email_to: e.target.value })}
          placeholder="team@example.com"
        />

        <div className="muted" style={{ fontSize: 12, marginTop: 4 }}>
          Alerts to this address are delivered via a Databricks notification
          destination. The destination is auto-created on first send if one
          doesn't already point at this address.
        </div>

        <h3 style={{ marginTop: 24 }}>Thresholds</h3>
        <table className="data">
          <tbody>
            {Object.keys(cfg.thresholds).map((k) => (
              <tr key={k}>
                <td style={{ fontFamily: 'monospace' }}>{k}</td>
                <td style={{ width: 140 }}>
                  <input
                    type="number"
                    step="0.01"
                    value={thrVal(k)}
                    onChange={(e) => {
                      const v = e.target.value;
                      setThr(k, v === '' ? 0 : Number(v));
                    }}
                  />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        <div className="muted" style={{ marginTop: 4 }}>
          Thresholds persist to the <code>po_monitor.config</code> table in the configured catalog.
        </div>
      </div>

      <div style={{ gridColumn: '1 / -1', display: 'flex', gap: 12, alignItems: 'center' }}>
        <button className="primary" onClick={save} disabled={!dirty || saving}>
          {saving ? 'Saving…' : 'Save'}
        </button>
        {msg && (
          <span
            className="muted"
            style={msg.kind === 'err' ? { color: '#f87171' } : undefined}
          >
            {msg.text}
          </span>
        )}
      </div>
    </div>
  );
}
