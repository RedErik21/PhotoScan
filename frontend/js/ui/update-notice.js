// Banner degli aggiornamenti: resta visibile in fondo alla pagina finche' non lo chiudi («Più tardi»).
// Stati: versione disponibile -> Scarica (o Apri su GitHub nell'eseguibile) -> pronta -> Installa al riavvio
// -> verrà installata alla prossima apertura (Riavvia ora nella finestra dell'app).
// Lo stato arriva da GET /api/update all'avvio, dagli eventi SSE `update` e, durante un download, da un polling breve.
// «Più tardi» vale per quella versione: una versione nuova fa ricomparire il banner.
import { h, icon } from './el.js';
import { api, onEvent } from '../api.js';
import { confirmDialog } from './dialog.js';
import { showSnackbar } from './snackbar.js';

const DISMISS_KEY = 'fs-update-dismissed';   // chiave dello stato chiuso con «Più tardi»
const INSTALLED_KEY = 'fs-update-installed'; // ultima installazione già annunciata (persistente)
const STYLE_ID = 'fs-update-banner-style';
const POLL_MS = 1500;
const SETTINGS_HASH = '#/settings?tab=updates';

const CSS = `
.fs-update-banner {
  position: fixed; left: 50%; bottom: var(--sp-5); transform: translateX(-50%);
  z-index: var(--z-toast); width: min(720px, calc(100vw - 2 * var(--sp-4)));
  display: flex; flex-wrap: wrap; align-items: center; gap: var(--sp-3) var(--sp-4);
  padding: var(--sp-3) var(--sp-4) var(--sp-3) var(--sp-5); border-radius: var(--shape-lg);
  background: var(--md-sys-color-surface-container-highest);
  color: var(--md-sys-color-on-surface);
  box-shadow: var(--md-sys-elevation-level3, 0 6px 18px rgba(0,0,0,.25));
  animation: fs-update-in 220ms ease-out;
}
.fs-update-banner__icon { color: var(--md-sys-color-primary); }
.fs-update-banner__text { flex: 1 1 260px; min-width: 0; margin: 0; }
.fs-update-banner__actions { display: flex; flex-wrap: wrap; align-items: center; gap: var(--sp-2); }
.fs-update-banner__progress { flex: 1 1 100%; }
@keyframes fs-update-in { from { opacity: 0; transform: translate(-50%, 12px); } to { opacity: 1; transform: translate(-50%, 0); } }
@media (prefers-reduced-motion: reduce) { .fs-update-banner { animation: none; } }
`;

let banner = null;
let lastState = null;
let pollTimer = null;
let busy = false;

function injectStyle() {
  if (document.getElementById(STYLE_ID)) return;
  const style = document.createElement('style');
  style.id = STYLE_ID;
  style.textContent = CSS;
  document.head.append(style);
}

/** La finestra dell'app (pywebview) sa riavviarsi; nel browser no. */
const canRestart = () => typeof window.pywebview?.api?.restart_app === 'function';

/** Cosa mostrare, o null. `key` decide se l'avviso e' gia' stato chiuso con «Più tardi». */
function model(st) {
  const job = st.download_job;
  const latest = st.check?.latest;
  // clone git: si aggiorna con git pull. Eseguibile: si scarica solo se la Release ha PhotoScan.exe
  const canDownload = !st.git_checkout && (!st.frozen || st.exe_ready);

  if (job && job.state === 'running') {
    const pct = job.total ? Math.round((100 * job.done) / job.total) : null;
    return { key: null, text: `Download di FotoScan ${latest?.version ?? ''} in corso${pct === null ? '…' : ` · ${pct}%`}`,
      progress: pct, actions: [] };
  }

  const p = st.pending;
  if (p) {
    const queued = p.approved || st.settings?.update_auto_install;
    if (queued) {
      const when = st.frozen ? 'alla chiusura di FotoScan' : 'alla prossima apertura';
      return { key: `queued-${p.version}`, text: `FotoScan ${p.version} verrà installato ${when}.`,
        actions: canRestart() ? [restartButton()] : [] };
    }
    return { key: `ready-${p.version}`, text: `FotoScan ${p.version} è pronto da installare.`,
      actions: [actionButton('Installa al riavvio', 'system_update_alt', approve, true)] };
  }

  if (st.check?.available && latest) {
    const v = latest.version;
    const actions = canDownload
      ? [actionButton('Scarica', 'download', download, true)]
      : [linkButton('Apri su GitHub', latest.html_url)];
    return { key: `avail-${v}`, text: `È disponibile FotoScan ${v}.`, actions };
  }
  return null;
}

const actionButton = (label, ic, onclick, filled = false) =>
  h(filled ? 'md-filled-tonal-button' : 'md-text-button', { onclick, disabled: busy },
    icon(ic, { slot: 'icon' }), label);
const linkButton = (label, href) => h('md-text-button', { href: href || SETTINGS_HASH, target: href ? '_blank' : undefined,
  rel: href ? 'noopener' : undefined }, icon('open_in_new', { slot: 'icon' }), label);
const restartButton = () => actionButton('Riavvia ora', 'restart_alt', restart, true);

async function refresh() {
  try {
    lastState = await api.get('/update');
  } catch {
    lastState = null; // server vecchio o non pronto: nessun banner
  }
  render();
}

function render() {
  const st = lastState;
  const m = st && !st.checking && !location.hash.startsWith(SETTINGS_HASH) ? model(st) : null;
  const dismissed = m?.key && localStorage.getItem(DISMISS_KEY) === m.key;
  if (!m || dismissed) {
    banner?.remove();
    banner = null;
    stopPolling();
    return;
  }
  if (!banner) {
    banner = h('div', { class: 'fs-update-banner', role: 'status', 'aria-live': 'polite' });
    document.body.append(banner);
  }
  // h() ignora i null, replaceChildren no: per questo i figli passano da h() e il filtro
  const progress = m.progress === null || m.progress === undefined ? null
    : h('md-linear-progress', { class: 'fs-update-banner__progress', value: m.progress / 100 });
  banner.replaceChildren(...[
    h('span', { class: 'fs-update-banner__icon material-symbols-outlined', 'aria-hidden': 'true' }, 'new_releases'),
    h('p', { class: 'fs-update-banner__text md-typescale-body-large' }, m.text),
    h('div', { class: 'fs-update-banner__actions' },
      ...m.actions,
      h('md-text-button', { onclick: () => dismiss(m.key) }, 'Più tardi'),
      h('md-text-button', { onclick: () => { location.hash = SETTINGS_HASH; } }, 'Dettagli')),
    progress,
  ].filter(Boolean));
  if (lastState?.download_job?.state === 'running') startPolling(); else stopPolling();
}

function dismiss(key) {
  if (key) localStorage.setItem(DISMISS_KEY, key);
  render();
}

function startPolling() {
  if (pollTimer) return;
  pollTimer = setInterval(refresh, POLL_MS);
}
function stopPolling() {
  if (pollTimer) clearInterval(pollTimer);
  pollTimer = null;
}

async function act(fn) {
  if (busy) return;
  busy = true;
  render();
  try {
    await fn();
  } catch (e) {
    showSnackbar(e.message || String(e), { error: true, duration: 6000 });
  } finally {
    busy = false;
    await refresh();
  }
}

const download = () => act(async () => {
  await api.post('/update/download');
  startPolling();
});

const approve = () => act(async () => {
  await api.post('/update/install');
});

const restart = () => act(async () => {
  const ok = await confirmDialog({
    title: 'Riavviare FotoScan ora?',
    message: 'FotoScan si chiude, installa la nuova versione e si riapre. Un’analisi in corso viene interrotta e riprende alla riapertura.',
    confirmLabel: 'Installa e riavvia',
  });
  if (!ok) return;
  showSnackbar('Riavvio di FotoScan…');
  await window.pywebview.api.restart_app();
});

function announceInstalled(st) {
  const li = st.last_install;
  if (li && li.to === st.current && localStorage.getItem(INSTALLED_KEY) !== li.to) {
    localStorage.setItem(INSTALLED_KEY, li.to);
    showSnackbar(`FotoScan è stato aggiornato alla versione ${li.to}.`, { duration: 6000 });
  }
}

function onState(st) {
  if (!st || st.checking) return;
  lastState = st;
  announceInstalled(st);
  render();
}

export function watchUpdates() {
  injectStyle();
  refresh();
  onEvent('update', onState);
  window.addEventListener('hashchange', render);
}
