// Store globale minimale (pub/sub). Chiavi note: status, settings, theme, selection.
const state = new Map();
const subs = new Map(); // key -> Set<fn>

export const store = {
  get: (key, fallback) => (state.has(key) ? state.get(key) : fallback),
  set(key, value) {
    state.set(key, value);
    (subs.get(key) || []).forEach((fn) => { try { fn(value); } catch (e) { console.error(e); } });
  },
  /** Chiama fn(value) a ogni cambio di key. Ritorna l'unsubscribe. */
  subscribe(key, fn) {
    if (!subs.has(key)) subs.set(key, new Set());
    subs.get(key).add(fn);
    return () => subs.get(key)?.delete(fn);
  },
};

// Tema: 'system' | 'light' | 'dark' (persistito in localStorage, sempre in try/catch)
export function applyTheme(mode) {
  const root = document.documentElement;
  if (mode === 'light' || mode === 'dark') root.setAttribute('data-theme', mode);
  else root.removeAttribute('data-theme');
  store.set('theme', mode);
  try { localStorage.setItem('fotoscan.theme', mode); } catch { /* ignora */ }
}

export function initTheme() {
  let mode = 'system';
  try { mode = localStorage.getItem('fotoscan.theme') || 'system'; } catch { /* ignora */ }
  applyTheme(mode);
}
