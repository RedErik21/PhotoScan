// Router a hash: #/percorso?chiave=valore. Carica le view in modo lazy (import dinamico).
import { routes } from './routes.js';
import { emptyState, errorState } from './ui/empty-state.js';

const listeners = new Set();
const prefetched = new Set();
let current = { path: '/', params: {}, query: {}, route: null };
let cleanup = null;
let abort = null;
let outlet = null;
let seq = 0;

/** Precarica il modulo JS di una route (es. su hover/focus dei link di navigazione). */
export function prefetchRoute(path) {
  const m = match(path);
  if (!m || prefetched.has(m.route.view)) return;
  prefetched.add(m.route.view);
  import(m.route.view).catch(() => { prefetched.delete(m.route.view); });
}

export function parseHash(hash = location.hash) {
  const raw = hash.replace(/^#/, '') || '/';
  const [p, q = ''] = raw.split('?');
  const query = Object.fromEntries(new URLSearchParams(q));
  return { path: decodeURI(p) || '/', query };
}

function match(path) {
  for (const r of routes) {
    const names = [];
    const re = new RegExp('^' + r.path.replace(/:[^/]+/g, (m) => { names.push(m.slice(1)); return '([^/]+)'; }) + '/?$');
    const m = path.match(re);
    if (m) return { route: r, params: Object.fromEntries(names.map((n, i) => [n, decodeURIComponent(m[i + 1])])) };
  }
  return null;
}

/** navigate('/folder', {name: '2022/08 - Mare'})  oppure navigate('/gallery') */
export function navigate(path, query) {
  const q = query ? '?' + new URLSearchParams(query).toString() : '';
  const target = `#${path}${q}`;
  if (location.hash === target) render(); else location.hash = target;
}

export function back() { history.length > 1 ? history.back() : navigate('/'); }
export function currentRoute() { return current; }
export function onRoute(fn) { listeners.add(fn); return () => listeners.delete(fn); }

async function render() {
  if (!outlet) return;
  const my = ++seq;
  // Un indirizzo con % non valido (es. #/%E0%A4%A) non deve bloccare la pagina: diventa "Pagina non trovata".
  let path = '/', query = {}, m = null;
  try { ({ path, query } = parseHash()); m = match(path); } catch (e) { console.warn('Indirizzo non valido:', e.message); }
  if (!m) {
    if (cleanup) { try { cleanup(); } catch (e) { console.error(e); } cleanup = null; }
    if (abort) abort.abort();
    abort = new AbortController();
    outlet.replaceChildren();
    outlet.scrollTop = 0;
    current = { path, query, params: {}, route: null };
    listeners.forEach((fn) => fn(current));
    document.title = 'PhotoScan';
    outlet.replaceChildren(emptyState({
      icon: 'search_off', title: 'Pagina non trovata', message: 'Questo indirizzo non esiste.',
      actionLabel: 'Vai alla Home', onAction: () => navigate('/'),
    }));
    return;
  }
  try {
    // Carica il modulo prima di svuotare l'outlet: evita il flash bianco al cambio pagina.
    prefetched.add(m.route.view);
    const mod = await import(m.route.view);
    if (my !== seq) return; // navigazione piu' recente
    if (cleanup) { try { cleanup(); } catch (e) { console.error(e); } cleanup = null; }
    if (abort) abort.abort();
    abort = new AbortController();
    outlet.replaceChildren();
    outlet.scrollTop = 0;
    current = { path, query, params: m.params || {}, route: m.route };
    listeners.forEach((fn) => fn(current));
    document.title = `${m.route.title} · PhotoScan`;
    const ret = await mod.mount(outlet, { path: path.length > 1 ? path.replace(/\/+$/, '') : path, route: m.route, params: m.params, query, navigate, signal: abort.signal });
    if (my !== seq) { if (typeof ret === 'function') ret(); return; }
    if (typeof ret === 'function') cleanup = ret;
  } catch (e) {
    if (my !== seq) return;
    console.error('Errore view', m.route.view, e);
    if (cleanup) { try { cleanup(); } catch (err) { console.error(err); } cleanup = null; }
    outlet.replaceChildren(errorState({
      title: 'Questa schermata non è disponibile',
      message: 'Ricarica la pagina o riprova tra un momento.',
      onRetry: () => render(),
    }));
  }
}

export function startRouter(outletEl) {
  outlet = outletEl;
  addEventListener('hashchange', render);
  render();
}
