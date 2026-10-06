import { Link } from 'react-router-dom';
import { reportUI } from '../api/telemetry';
import React, { useEffect, useState } from 'react';

// options: { tone: 'warning' | 'priority', title, duration (ms) }
export function notify(message, error = false, requestId = null, options = {}) {
  window.dispatchEvent(new CustomEvent('artist-notice', { detail: { message, error, requestId, ...options } }));
}

const TONES = {
  error: 'border-red-800 text-red-200',
  info: 'border-blue-800 text-blue-100',
  warning: 'border-amber-700/80 text-amber-100',
  priority: 'border-rose-600 bg-rose-950/80 text-rose-50 ring-2 ring-rose-500/40',
};

export function Notifications() {
  const [items, setItems] = useState([]);
  useEffect(() => {
    const timers = new Set();
    const receive = event => {
      const id = crypto.randomUUID();
      setItems(current => [...current.filter(item => item.message !== event.detail.message), { ...event.detail, id }].slice(-4));
      const timer = setTimeout(() => { setItems(current => current.filter(item => item.id !== id)); timers.delete(timer); }, event.detail.duration || (event.detail.error ? 12000 : 5000));
      timers.add(timer);
    };
    window.addEventListener('artist-notice', receive);
    return () => { window.removeEventListener('artist-notice', receive); timers.forEach(clearTimeout); };
  }, []);
  return <div aria-live="polite" className="fixed bottom-4 right-4 z-[70] space-y-2 max-w-sm">
    {items.map(item => <div key={item.id} role={item.error ? 'alert' : 'status'} className={`flex gap-3 rounded border p-3 text-sm shadow-xl bg-[#0c1219] ${TONES[item.error ? 'error' : item.tone || 'info']}`}>
      <span className="whitespace-pre-line">{item.title && <b className="block">{item.title}</b>}{item.message}{item.requestId && <Link className="block underline mt-1" to={`/logs?request_id=${encodeURIComponent(item.requestId)}`}>Open error diagnostics</Link>}</span><button aria-label="Dismiss notification" onClick={() => setItems(current => current.filter(other => other.id !== item.id))}>×</button>
    </div>)}
  </div>;
}

export class ErrorBoundary extends React.Component {
  state = { failed: false, version: 0 };
  static getDerivedStateFromError() { return { failed: true }; }
  componentDidCatch(error) { reportUI('error', error.message); }
  render() {
    if (this.state.failed) return <div role="alert" className="m-4 rounded border border-red-900 bg-[#0c1219] p-6 text-slate-200">
      <h2 className="font-semibold">This view could not be displayed</h2>
      <p className="my-3 text-sm">Your library is still stored on the backend. Try reopening this view or reload the app.</p>
      <button className="mr-4 text-blue-300" onClick={() => this.setState(state => ({ failed: false, version: state.version + 1 }))}>Try again</button>
      <button onClick={() => window.location.reload()}>Reload app</button>
    </div>;
    return <React.Fragment key={this.state.version}>{this.props.children}</React.Fragment>;
  }
}
