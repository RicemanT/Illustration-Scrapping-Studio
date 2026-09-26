import { backendBase, proxyHeaders } from './runtime';
const endpoint = `${backendBase}/api/diagnostics/client`;
let last = 0;
export function reportUI(kind, message) {
  // Bound UI traffic; no form values, clipboard data, query strings or image contents.
  if (kind !== 'error' && Date.now() - last < 300) return;
  last = Date.now();
  fetch(endpoint, { method: 'POST', headers: { 'Content-Type': 'application/json', ...proxyHeaders() }, body: JSON.stringify({ kind, message: String(message).slice(0, 1000), path: location.pathname.slice(0, 300) }) }).catch(() => {});
}
export function installTelemetry() {
  window.addEventListener('error', event => reportUI('error', event.message || 'Browser resource failed to load'));
  window.addEventListener('unhandledrejection', event => reportUI('error', event.reason?.message || 'Unhandled browser operation failure'));
  document.addEventListener('click', event => {
    const button = event.target.closest?.('button, a');
    if (button && !location.pathname.startsWith('/logs')) reportUI('action', button.getAttribute('aria-label') || button.textContent.trim().slice(0, 120) || 'Control activated');
  }, true);
}
