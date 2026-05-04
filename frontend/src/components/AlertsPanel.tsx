import { useEffect, useMemo, useState } from 'react';
import { api, AlertEvent, AlertRule, AlertRuleType, AlertTestResult } from '../lib/api';
import { Toggle } from './Toggle';

const RULE_TYPES: { value: AlertRuleType; label: string }[] = [
  { value: 'PO_QUARANTINED', label: 'PO quarantined (3 failures + 3d inactive)' },
  { value: 'OPTIMIZE_FAILURE_RATE', label: 'OPTIMIZE failure rate > X' },
  { value: 'VACUUM_STALE', label: "VACUUM hasn't succeeded in > N days" },
  { value: 'UNCLUSTERED_BYTES', label: 'Unclustered bytes > X% of table' },
  { value: 'AVG_FILE_SIZE_DROP', label: 'Avg file size dropped > Y% WoW' },
  { value: 'MERGE_CONFLICT_SPIKE', label: 'MERGE conflict spike (> X / window)' },
];

function ago(ts: string | null | undefined): string {
  if (!ts) return '—';
  const t = Date.parse(ts);
  if (Number.isNaN(t)) return '—';
  const sec = Math.max(0, Math.floor((Date.now() - t) / 1000));
  if (sec < 60) return `${sec}s ago`;
  if (sec < 3600) return `${Math.floor(sec / 60)}m ago`;
  if (sec < 86400) return `${Math.floor(sec / 3600)}h ago`;
  return `${Math.floor(sec / 86400)}d ago`;
}

function StatusPill({ status }: { status?: string | null }) {
  const s = (status || '').toUpperCase();
  const color =
    s === 'FIRED' ? '#ef4444' :
    s === 'OK' ? '#22c55e' :
    s === 'ERROR' ? '#f59e0b' :
    '#6b7280';
  const bg =
    s === 'FIRED' ? 'rgba(239,68,68,0.15)' :
    s === 'OK' ? 'rgba(34,197,94,0.15)' :
    s === 'ERROR' ? 'rgba(245,158,11,0.15)' :
    'rgba(107,114,128,0.15)';
  return (
    <span style={{
      display: 'inline-block', padding: '2px 8px', borderRadius: 999,
      fontSize: 11, fontWeight: 600, color, background: bg,
    }}>
      {s || '—'}
    </span>
  );
}

function targetLabel(r: { catalog?: string | null; schema_name?: string | null; table_name?: string | null }): string {
  if (!r.catalog && !r.schema_name && !r.table_name) return 'global';
  return `${r.catalog || '*'}.${r.schema_name || '*'}.${r.table_name || '*'}`;
}

function maskWebhook(w?: string | null): string {
  if (!w) return '—';
  if (w.length <= 30) return w;
  return w.slice(0, 30) + '…';
}

function TestModal({ rule, results, onClose }: { rule: AlertRule; results: AlertTestResult[]; onClose: () => void }) {
  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal" onClick={(e) => e.stopPropagation()} style={{ maxWidth: 720, width: '90%' }}>
        <div className="modal-header">
          <h3>Test results — {rule.rule_type}</h3>
          <button onClick={onClose}>×</button>
        </div>
        <div style={{ padding: 12, maxHeight: '60vh', overflowY: 'auto' }}>
          {results.length === 0 ? (
            <div className="empty-state">No targets matched. (Wildcard rule with no matching tables, perhaps.)</div>
          ) : (
            <table className="data" style={{ width: '100%' }}>
              <thead>
                <tr><th>Target</th><th>Triggered</th><th>Observed</th><th>Message</th><th>Would dispatch</th></tr>
              </thead>
              <tbody>
                {results.map((r, i) => (
                  <tr key={i}>
                    <td><code>{r.target}</code></td>
                    <td><StatusPill status={r.triggered ? 'FIRED' : 'OK'} /></td>
                    <td>{typeof r.observed === 'number' ? r.observed : '—'}</td>
                    <td className="muted" style={{ maxWidth: 280 }}>{r.message}</td>
                    <td className="muted" style={{ fontSize: 11 }}>{r.would_dispatch?.summary || '—'}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      </div>
    </div>
  );
}

function EditModal({ rule, onSave, onClose }: {
  rule: AlertRule;
  onSave: (patch: Partial<AlertRule>) => Promise<void>;
  onClose: () => void;
}) {
  const [threshold, setThreshold] = useState(rule.threshold);
  const [lookback, setLookback] = useState(rule.lookback_minutes);
  const [webhook, setWebhook] = useState(rule.slack_webhook || '');
  const [email, setEmail] = useState(rule.email || '');
  const [saving, setSaving] = useState(false);

  const save = async () => {
    setSaving(true);
    try {
      await onSave({
        threshold: Number(threshold),
        lookback_minutes: Number(lookback),
        slack_webhook: webhook || undefined,
        email: email || undefined,
      });
      onClose();
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal" onClick={(e) => e.stopPropagation()}>
        <div className="modal-header">
          <h3>Edit rule</h3>
          <button onClick={onClose}>×</button>
        </div>
        <div style={{ padding: 12 }}>
          <div className="section-label">Type</div>
          <div className="muted" style={{ marginBottom: 8 }}>{rule.rule_type}</div>

          <div className="section-label">Target</div>
          <div className="muted" style={{ marginBottom: 8 }}>{targetLabel(rule)}</div>

          <div className="section-label">Threshold</div>
          <input type="number" step="0.01" value={threshold}
            onChange={(e) => setThreshold(Number(e.target.value))} />

          <div className="section-label">Lookback (minutes)</div>
          <input type="number" value={lookback}
            onChange={(e) => setLookback(Number(e.target.value))} />

          <div className="section-label">Slack webhook (overrides global)</div>
          <input type="text" placeholder="https://hooks.slack.com/services/..." value={webhook}
            onChange={(e) => setWebhook(e.target.value)} />

          <div className="section-label">Email</div>
          <input type="email" placeholder="alerts@example.com" value={email}
            onChange={(e) => setEmail(e.target.value)} />

          <div style={{ marginTop: 12, display: 'flex', gap: 8 }}>
            <button className="primary" onClick={save} disabled={saving}>
              {saving ? 'Saving…' : 'Save'}
            </button>
            <button onClick={onClose}>Cancel</button>
          </div>
        </div>
      </div>
    </div>
  );
}

export function AlertsPanel() {
  const [rules, setRules] = useState<AlertRule[]>([]);
  const [events, setEvents] = useState<AlertEvent[]>([]);
  const [form, setForm] = useState<any>({
    rule_type: 'PO_QUARANTINED',
    threshold: 3,
    lookback_minutes: 60,
    enabled: true,
    catalog: '',
    schema_name: '',
    table_name: '',
    slack_webhook: '',
    email: '',
  });
  const [editing, setEditing] = useState<AlertRule | null>(null);
  const [testFor, setTestFor] = useState<{ rule: AlertRule; results: AlertTestResult[] } | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const refresh = async () => {
    try {
      const r = await api.listAlerts();
      setRules(r.rules);
      const ev = await api.listAlertEvents({ limit: 20 });
      setEvents(ev.events);
    } catch (e: any) {
      setError(e.message || String(e));
    }
  };
  useEffect(() => { refresh(); }, []);

  const add = async () => {
    setError(null);
    try {
      await api.createAlert(form);
      setForm({ ...form, catalog: '', schema_name: '', table_name: '' });
      refresh();
    } catch (e: any) {
      setError(e.message || String(e));
    }
  };

  const toggle = async (r: AlertRule) => {
    setBusy(r.rule_id);
    try {
      await api.patchAlert(r.rule_id, { enabled: !r.enabled });
      refresh();
    } catch (e: any) {
      setError(e.message || String(e));
    } finally {
      setBusy(null);
    }
  };

  const del = async (r: AlertRule) => {
    if (!confirm(`Delete this ${r.rule_type} rule?`)) return;
    setBusy(r.rule_id);
    try {
      await api.deleteAlert(r.rule_id);
      refresh();
    } catch (e: any) {
      setError(e.message || String(e));
    } finally {
      setBusy(null);
    }
  };

  const runTest = async (r: AlertRule) => {
    setBusy(r.rule_id);
    setError(null);
    try {
      const res = await api.testAlert(r.rule_id);
      setTestFor({ rule: r, results: res.results });
    } catch (e: any) {
      setError(e.message || String(e));
    } finally {
      setBusy(null);
    }
  };

  const saveEdit = async (patch: Partial<AlertRule>) => {
    if (!editing) return;
    await api.patchAlert(editing.rule_id, patch);
    refresh();
  };

  const ruleById = useMemo(() => {
    const m: Record<string, AlertRule> = {};
    for (const r of rules) m[r.rule_id] = r;
    return m;
  }, [rules]);

  return (
    <div>
      <div className="caveat-banner" style={{ marginBottom: 12 }}>
        Alert evaluation runs as the app service principal. The SP needs SELECT on the
        target tables and on <code>system.storage.*</code> / <code>system.query.history</code>.
        Eval cadence is 60s.
      </div>

      {error && (
        <div className="alert-banner" style={{ marginBottom: 12 }}>{error}</div>
      )}

      <div className="grid" style={{ gridTemplateColumns: '1fr 1fr', gap: 12 }}>
        <div className="card">
          <h3>Create alert rule</h3>

          <div className="section-label">Rule type</div>
          <select value={form.rule_type} onChange={(e) => setForm({ ...form, rule_type: e.target.value })}>
            {RULE_TYPES.map((t) => (
              <option key={t.value} value={t.value}>{t.label}</option>
            ))}
          </select>

          <div className="section-label">Threshold</div>
          <input type="number" step="0.01" value={form.threshold}
            onChange={(e) => setForm({ ...form, threshold: Number(e.target.value) })} />

          <div className="section-label">Lookback (minutes)</div>
          <input type="number" value={form.lookback_minutes}
            onChange={(e) => setForm({ ...form, lookback_minutes: Number(e.target.value) })} />

          <div className="section-label">Scope (leave blank for wildcard)</div>
          <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr 1fr', gap: 6 }}>
            <input placeholder="catalog" value={form.catalog}
              onChange={(e) => setForm({ ...form, catalog: e.target.value })} />
            <input placeholder="schema" value={form.schema_name}
              onChange={(e) => setForm({ ...form, schema_name: e.target.value })} />
            <input placeholder="table" value={form.table_name}
              onChange={(e) => setForm({ ...form, table_name: e.target.value })} />
          </div>

          <div className="section-label">Slack webhook (optional)</div>
          <input type="text" placeholder="https://hooks.slack.com/services/..." value={form.slack_webhook}
            onChange={(e) => setForm({ ...form, slack_webhook: e.target.value })} />

          <div className="section-label">Email (optional)</div>
          <input type="email" placeholder="alerts@example.com" value={form.email}
            onChange={(e) => setForm({ ...form, email: e.target.value })} />

          <div style={{ marginTop: 12 }}>
            <button className="primary" onClick={add}>Add rule</button>
          </div>
        </div>

        <div className="card">
          <h3>Current rules ({rules.length})</h3>
          {rules.length === 0 ? (
            <div className="empty-state">No rules yet.</div>
          ) : (
            <table className="data" style={{ fontSize: 12 }}>
              <thead>
                <tr>
                  <th>Type</th>
                  <th>Target</th>
                  <th>Thr.</th>
                  <th>Status</th>
                  <th>Last eval</th>
                  <th>Last fired</th>
                  <th>Webhook</th>
                  <th>Email</th>
                  <th>On</th>
                  <th></th>
                </tr>
              </thead>
              <tbody>
                {rules.map((r) => (
                  <tr key={r.rule_id} title={r.last_error || ''}>
                    <td className="muted">{r.rule_type.replace(/_/g, ' ').toLowerCase()}</td>
                    <td>{targetLabel(r)}</td>
                    <td>{r.threshold}</td>
                    <td><StatusPill status={r.last_status} /></td>
                    <td className="muted">{ago(r.last_evaluated_at)}</td>
                    <td className="muted">{ago(r.last_fired_at)}</td>
                    <td className="muted" style={{ maxWidth: 140, overflow: 'hidden', textOverflow: 'ellipsis' }}>
                      {maskWebhook(r.slack_webhook_masked || r.slack_webhook)}
                    </td>
                    <td className="muted">{r.email || '—'}</td>
                    <td>
                      <Toggle checked={!!r.enabled} onChange={() => toggle(r)} disabled={busy === r.rule_id} />
                    </td>
                    <td style={{ whiteSpace: 'nowrap' }}>
                      <button onClick={() => runTest(r)} disabled={busy === r.rule_id} title="Evaluate now (no delivery)">Test</button>
                      {' '}
                      <button onClick={() => setEditing(r)} disabled={busy === r.rule_id}>Edit</button>
                      {' '}
                      <button onClick={() => del(r)} disabled={busy === r.rule_id}>×</button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      </div>

      <div className="card" style={{ marginTop: 12 }}>
        <h3>Recent fires ({events.length})</h3>
        {events.length === 0 ? (
          <div className="empty-state">No fires yet. Rules evaluate every 60 seconds.</div>
        ) : (
          <table className="data" style={{ fontSize: 12 }}>
            <thead>
              <tr>
                <th>When</th>
                <th>Rule</th>
                <th>Target</th>
                <th>Observed</th>
                <th>Threshold</th>
                <th>Message</th>
                <th>Delivery</th>
              </tr>
            </thead>
            <tbody>
              {events.map((e) => {
                const owner = ruleById[e.rule_id];
                return (
                  <tr key={e.event_id}>
                    <td className="muted">{ago(e.fired_at)}</td>
                    <td className="muted">{(owner?.rule_type || e.rule_type).replace(/_/g, ' ').toLowerCase()}</td>
                    <td><code>{targetLabel(e)}</code></td>
                    <td>{e.observed_value}</td>
                    <td>{e.threshold}</td>
                    <td className="muted" style={{ maxWidth: 360 }}>{e.message}</td>
                    <td className="muted" style={{ fontSize: 11 }}>{e.delivery || '—'}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}
      </div>

      {editing && (
        <EditModal
          rule={editing}
          onClose={() => setEditing(null)}
          onSave={saveEdit}
        />
      )}
      {testFor && (
        <TestModal
          rule={testFor.rule}
          results={testFor.results}
          onClose={() => setTestFor(null)}
        />
      )}
    </div>
  );
}
