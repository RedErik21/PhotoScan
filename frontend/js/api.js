// Client API + eventi SSE. Unico punto di accesso al backend.
export class ApiError extends Error {
  constructor(status, message, body) { super(message); this.status = status; this.body = body; }
}

function qs(params) {
  const u = new URLSearchParams();
  for (const [k, v] of Object.entries(params || {})) {
    if (v === undefined || v === null || v === '') continue;
    u.set(k, String(v));
  }
  const s = u.toString();
  return s ? `?${s}` : '';
}

async function request(method, path, { params, body, signal } = {}) {
  const opts = { method, signal, headers: {} };
  if (body !== undefined) { opts.headers['Content-Type'] = 'application/json'; opts.body = JSON.stringify(body); }
  let res;
  try { res = await fetch(`/api${path}${qs(params)}`, opts); }
  catch (e) { if (e.name === 'AbortError') throw e; throw new ApiError(0, 'Server non raggiungibile'); }
  const text = await res.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch { data = text; }
  if (!res.ok) throw new ApiError(res.status, errorMessage(res.status, data), data);
  return data;
}

const STATUS_IT = {
  400: 'Richiesta non valida.', 401: 'Accesso non consentito.', 403: 'Operazione non consentita.',
  404: 'Elemento non trovato.', 409: 'Operazione già in corso o in conflitto con lo stato attuale.',
  413: 'Contenuto troppo grande.', 422: 'Dati non validi.', 429: 'Troppe richieste, riprova tra poco.',
  500: 'Errore interno del programma.', 502: 'Il server non risponde.', 503: 'Servizio non ancora pronto, riprova tra poco.',
};

/** Messaggio sempre in italiano: usa `detail` del server (stringa o lista di errori), altrimenti un testo per codice HTTP. */
function errorMessage(status, data) {
  const d = data && data.detail;
  if (typeof d === 'string' && d.trim()) return d;
  if (Array.isArray(d) && d.length) {
    const first = d[0];
    const where = Array.isArray(first?.loc) ? first.loc.filter((x) => x !== 'body' && x !== 'query').join('.') : '';
    return where ? `Valore non valido per «${where}».` : 'Dati non validi.';
  }
  return STATUS_IT[status] || `Errore (${status}).`;
}

export const api = {
  get: (path, params, signal) => request('GET', path, { params, signal }),
  post: (path, body) => request('POST', path, { body: body ?? {} }),
  put: (path, body) => request('PUT', path, { body: body ?? {} }),
  patch: (path, body) => request('PATCH', path, { body: body ?? {} }),
  // del(path, params?, body?): alcune DELETE (es. /albums/{id}/items) vogliono un corpo JSON.
  del: (path, params, body) => request('DELETE', path, { params, body }),
};

// URL di risorse binarie (usare in <img src>, <video src>)
export const fileUrl = {
  thumb: (id, size = 256) => `/api/files/${id}/thumb?size=${size}`,
  /** Come thumb, ma 404 se non c'e' un'immagine vera (niente segnaposto): per onerror. */
  thumbStrict: (id, size = 256) => `/api/files/${id}/thumb?size=${size}&strict=1`,
  display: (id, max = 2048) => `/api/files/${id}/display?max=${max}`,
  media: (id) => `/api/files/${id}/media`,
  faceCrop: (id, size = 256) => `/api/faces/${id}/crop?size=${size}`,
};

// --- Eventi SSE (/api/events) con riconnessione automatica ---
const handlers = new Map(); // type -> Set<fn>
let es = null;
let retry = 1000;

function connect() {
  if (es) return;
  es = new EventSource('/api/events');
  es.onopen = () => { retry = 1000; dispatch('connection', { online: true }); };
  es.onerror = () => {
    dispatch('connection', { online: false });
    es.close(); es = null;
    setTimeout(connect, retry); retry = Math.min(retry * 2, 10000);
  };
  for (const type of ['job', 'index', 'hello', 'update']) {
    es.addEventListener(type, (ev) => { try { dispatch(type, JSON.parse(ev.data)); } catch { /* ignora */ } });
  }
}

function dispatch(type, data) {
  (handlers.get(type) || []).forEach((fn) => { try { fn(data); } catch (e) { console.error(e); } });
}

/** Sottoscrive un tipo di evento ('job' | 'index' | 'update' | 'connection'). Ritorna la funzione per annullare. */
export function onEvent(type, fn) {
  if (!handlers.has(type)) handlers.set(type, new Set());
  handlers.get(type).add(fn);
  connect();
  return () => handlers.get(type)?.delete(fn);
}
