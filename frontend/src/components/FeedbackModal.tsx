import { useState } from 'react';
import { api } from '../lib/api';

type Props = {
  userEmail: string | null;
  onClose: () => void;
};

export function FeedbackModal({ userEmail, onClose }: Props) {
  void userEmail; // sent via X-Forwarded-Email header, not needed in payload
  const [subject, setSubject] = useState('PO Monitor feedback');
  const [message, setMessage] = useState('');
  const [sending, setSending] = useState(false);
  const [result, setResult] = useState<{ ok: boolean; msg: string } | null>(null);

  const send = async () => {
    if (!message.trim()) return;
    setSending(true);
    setResult(null);
    try {
      const res = await api.sendFeedback({
        subject,
        message,
        app_url: window.location.origin,
        user_agent: navigator.userAgent,
      });
      setResult({
        ok: true,
        msg: res.delivered
          ? 'Sent. Maintainer has been notified.'
          : 'Recorded. Maintainer will see it on next review.',
      });
      setMessage('');
      setTimeout(onClose, 1600);
    } catch (e: any) {
      setResult({ ok: false, msg: `Failed: ${e.message}` });
    } finally {
      setSending(false);
    }
  };

  return (
    <div className="modal-backdrop" onClick={(e) => { if (e.target === e.currentTarget) onClose(); }}>
      <div className="card" style={{ width: 480, maxWidth: '90vw', margin: 0 }}>
        <h3 style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
          <span>Send feedback</span>
          <button className="btn" onClick={onClose}>Close</button>
        </h3>

        <div className="muted" style={{ fontSize: 12, marginBottom: 12 }}>
          Goes to the app maintainer. Your email is attached automatically.
        </div>

        <div className="section-label">Subject</div>
        <input
          type="text"
          value={subject}
          onChange={(e) => setSubject(e.target.value)}
          style={{ width: '100%', marginBottom: 12 }}
        />

        <div className="section-label">Message</div>
        <textarea
          rows={7}
          value={message}
          onChange={(e) => setMessage(e.target.value)}
          placeholder="What's the issue, suggestion, or kudos?"
          style={{ width: '100%', minHeight: 140, fontFamily: 'inherit', fontSize: 13, padding: 8, resize: 'vertical' }}
        />

        {result && (
          <div className={result.ok ? 'stub' : 'spike'} style={{ marginTop: 12 }}>
            {result.msg}
          </div>
        )}

        <div className="action-row" style={{ marginTop: 16, justifyContent: 'flex-end' }}>
          <button className="btn" onClick={onClose}>Cancel</button>
          <button
            className="btn primary"
            disabled={sending || !message.trim()}
            onClick={send}
          >
            {sending ? 'Sending…' : 'Send'}
          </button>
        </div>
      </div>
    </div>
  );
}
