// Avviso breve sugli aggiornamenti (versione nuova, pronta, installata), una volta per stato.
// Lo stato arriva da GET /api/update all'avvio e dagli eventi SSE `update` (controllo all'avvio del server).
import { api, onEvent } from '../api.js';
import { showSnackbar } from './snackbar.js';

const SESSION_KEY = 'fs-update-notice';      // ultimo avviso mostrato in questa sessione
const INSTALLED_KEY = 'fs-update-installed'; // ultima installazione già annunciata (persistente)

function notice(st) {
  const p = st.pending;
  if (p) {
    const queued = p.approved || st.settings?.update_auto_install;
    return queued
      ? { key: `queued-${p.version}`, text: `FotoScan ${p.version} verrà installato alla prossima apertura.` }
      : { key: `ready-${p.version}`, text: `FotoScan ${p.version} è pronto da installare.` };
  }
  if (st.check?.available && !st.download_job) {
    return { key: `avail-${st.check.latest.version}`, text: `È disponibile FotoScan ${st.check.latest.version}.` };
  }
  const li = st.last_install;
  if (li && li.to === st.current && localStorage.getItem(INSTALLED_KEY) !== li.to) {
    localStorage.setItem(INSTALLED_KEY, li.to);
    return { key: `done-${li.to}`, text: `FotoScan è stato aggiornato alla versione ${li.to}.` };
  }
  return null;
}

function show(st) {
  if (!st || st.checking || location.hash.startsWith('#/settings?tab=updates')) return;
  const n = notice(st);
  if (!n || sessionStorage.getItem(SESSION_KEY) === n.key) return;
  sessionStorage.setItem(SESSION_KEY, n.key);
  showSnackbar(n.text, {
    actionLabel: 'Dettagli',
    onAction: () => { location.hash = '#/settings?tab=updates'; },
    duration: 8000,
  });
}

export function watchUpdates() {
  api.get('/update').then(show).catch(() => { /* server vecchio o non pronto: nessun avviso */ });
  onEvent('update', show);
}
