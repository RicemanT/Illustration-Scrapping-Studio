// The combined server injects this prefix; development defaults to the origin root.
export const appBase = document.querySelector('meta[name="studio-base"]')?.content || '';
export const backendBase = (import.meta.env.VITE_BACKEND_URL || appBase).replace(/\/$/, '');

// Jupyter protects state-changing requests with its same-origin XSRF cookie.
export function proxyHeaders() {
  if (!appBase || new URL(backendBase || '/', location.href).origin !== location.origin) return {};
  const value = document.cookie.split('; ').find(item => item.startsWith('_xsrf='))?.slice(6);
  if (!value) return {};
  try { return { 'X-XSRFToken': decodeURIComponent(value) }; } catch { return {}; }
}
