// Impostazioni > Aggiornamenti: versione installata, ricerca, download e installazione degli aggiornamenti,
// opzioni automatiche. Dati da GET /api/update (+ eventi SSE `update` e `job` per il download).
// Le opzioni si salvano subito (PUT /settings con il solo campo cambiato): la sezione non usa la barra «Salva».
// L'installazione avviene al riavvio: «Installa e riavvia» approva (POST /update/install) e chiede alla finestra
// dell'app (pywebview) di riaprirsi; nel browser si chiede di chiudere e riaprire FotoScan.
import { h, icon } from '../ui/el.js';
import { api, onEvent } from '../api.js';
import { confirmDialog } from '../ui/dialog.js';
import { skeletonList } from '../ui/skeleton.js';
import { showSnackbar } from '../ui/snackbar.js';
import { formatDateTime } from '../ui/format.js';

const JOB = 'update_download';
const STYLE_ID = 'fs-settings-updates-style';
const CSS = `
.upd-group { display: flex; flex-direction: column; gap: var(--sp-3); min-width: 0; }
.upd-group__title {
  margin: 0; color: var(--md-sys-color-on-surface);
  font: 500 var(--md-sys-typescale-title-medium-size) / var(--md-sys-typescale-title-medium-line-height) var(--md-ref-typeface-plain);
}
.upd-card {
  display: flex; flex-direction: column; gap: var(--sp-4); min-width: 0;
  padding: var(--sp-5) var(--sp-6); border-radius: var(--shape-lg);
  border: 1px solid var(--md-sys-color-outline-variant);
  background: var(--md-sys-color-surface-container); color: var(--md-sys-color-on-surface);
}
.upd-hint { margin: 0; color: var(--md-sys-color-on-surface-variant); }
.upd-head { display: flex; flex-wrap: wrap; align-items: center; gap: var(--sp-3) var(--sp-5); }
.upd-version { display: flex; flex-direction: column; gap: 2px; flex: 1 1 220px; min-width: 0; }
.upd-version__num { font-size: var(--md-sys-typescale-headline-small-size); font-variant-numeric: tabular-nums; }
.upd-actions { display: flex; flex-wrap: wrap; align-items: center; gap: var(--sp-2); }
.upd-notes {
  margin: 0; max-height: 280px; overflow: auto; white-space: pre-wrap; overflow-wrap: anywhere;
  padding: var(--sp-3) var(--sp-4); border-radius: var(--shape-sm);
  background: var(--md-sys-color-surface-container-high); font: inherit; font-size: 0.92em;
}
.upd-progress { display: flex; flex-direction: column; gap: var(--sp-2); }
.upd-switch { display: flex; align-items: center; justify-content: space-between; gap: var(--sp-4); padding: var(--sp-2) 0; }
.upd-switch + .upd-switch { border-top: 1px solid var(--md-sys-color-outline-variant); padding-top: var(--sp-3); }
.upd-switch__text { display: flex; flex-direction: column; min-width: 0; }
.callout--ok { background: color-mix(in srgb, #2e7d32 16%, var(--md-sys-color-surface-container-high)); color: var(--md-sys-color-on-surface); }
@media (pointer: coarse) { .upd-switch md-switch { min-height: 48px; } }
`;

function injectStyle() {
  if (document.getElementById(STYLE_ID)) return;
  const style = document.createElement('style');
  style.id = STYLE_ID;
  style.textContent = CSS;
  document.head.append(style);
}

const hint = (text) => h('p', { class: 'upd-hint md-typescale-body-medium' }, text);
const callout = (kind, ic, title, ...body) => h('div', { class: `callout callout--${kind}`, role: 'status' },
  h('span', { class: 'callout__icon material-symbols-outlined', 'aria-hidden': 'true' }, ic),
  h('div', {}, h('p', { class: 'callout__title' }, title), ...body));
const mb = (bytes) => `${(bytes / 1e6).toFixed(1).replace('.', ',')} MB`;

/** La finestra dell'app (pywebview) sa riavviarsi; nel browser no. */
const canRestart = () => typeof window.pywebview?.api?.restart_app === 'function';

const OPTIONS = [
  { key: 'update_check_on_start', title: 'Cerca aggiornamenti all’avvio',
    hint: 'A ogni apertura FotoScan chiede a GitHub se c’è una versione nuova. Viene inviata solo la richiesta dell’elenco delle versioni: nessuna foto, nessun dato del catalogo.' },
  { key: 'update_auto_download', title: 'Scarica automaticamente',
    hint: 'Quando trova una versione nuova la scarica subito, senza installarla.' },
  { key: 'update_auto_install', title: 'Installa automaticamente',
    hint: 'La versione scaricata viene installata da sola alla prossima apertura di FotoScan. Prima viene fatta una copia di riserva dei file del programma; le foto, il catalogo e le impostazioni non vengono toccati.' },
  { key: 'update_prerelease', title: 'Includi le versioni di prova',
    hint: 'Considera anche le pre-release: novità in anticipo, ma meno collaudate.' },
];

export async function mountSection(container) {
  injectStyle();
  let disposed = false;
  let data = null;
  let busy = '';            // azione in corso: check | download | install | discard
  const saving = new Set();

  const statusSlot = h('section', { class: 'upd-group' });
  const optionsSlot = h('section', { class: 'upd-group' });
  container.replaceChildren(skeletonList(3));

  async function load() {
    try {
      const d = await api.get('/update');
      if (disposed) return;
      data = d;
      paint();
    } catch (e) {
      if (disposed) return;
      container.replaceChildren(hint(`Impossibile leggere lo stato degli aggiornamenti: ${e.message}`));
    }
  }

  async function run(kind, fn) {
    if (busy) return;
    busy = kind;
    paint();
    try {
      await fn();
    } catch (e) {
      showSnackbar(e.message, { error: true, duration: 6000 });
    } finally {
      busy = '';
      if (!disposed) await load();
    }
  }

  const check = () => run('check', async () => {
    const d = await api.post('/update/check');
    if (disposed) return;
    data = d;
    const c = d.check || {};
    if (c.error) showSnackbar(c.error, { error: true, duration: 6000 });
    else if (c.available) showSnackbar(`È disponibile FotoScan ${c.latest.version}.`);
    else showSnackbar('FotoScan è aggiornato.');
  });

  const download = () => run('download', async () => { await api.post('/update/download'); });

  const discard = () => run('discard', async () => {
    const ok = await confirmDialog({
      title: 'Annullare l’aggiornamento?',
      message: 'La versione scaricata viene eliminata. Potrai scaricarla di nuovo quando vuoi.',
      confirmLabel: 'Annulla aggiornamento', cancelLabel: 'Indietro',
    });
    if (ok) await api.del('/update/pending');
  });

  const install = () => run('install', async () => {
    const v = data.pending?.version;
    const restartable = canRestart();
    const ok = await confirmDialog({
      title: restartable ? `Installare FotoScan ${v}?` : `Preparare l’installazione di FotoScan ${v}?`,
      message: restartable
        ? 'FotoScan si chiude, installa la nuova versione e si riapre. Un’analisi in corso viene interrotta e riprende alla riapertura.'
        : (data.frozen ? 'L’aggiornamento verrà installato quando chiudi FotoScan: poi l’app si riapre da sola.'
          : 'L’aggiornamento verrà installato quando chiudi e riapri FotoScan.'),
      confirmLabel: restartable ? 'Installa e riavvia' : 'Va bene',
    });
    if (!ok || disposed) return;
    await api.post('/update/install');
    if (restartable) {
      showSnackbar('Riavvio di FotoScan…');
      await window.pywebview.api.restart_app();
    } else {
      showSnackbar(data.frozen ? 'Chiudi FotoScan: l’aggiornamento si installa e l’app si riapre.'
        : 'Chiudi e riapri FotoScan per completare l’aggiornamento.', { duration: 8000 });
    }
  });

  async function setOption(key, value, sw) {
    saving.add(key);
    sw.disabled = true;
    try {
      await api.put('/settings', { [key]: value });
      if (disposed) return;
      data.settings[key] = value;
      showSnackbar('Impostazione salvata.');
    } catch (e) {
      sw.selected = !value;
      showSnackbar(e.message, { error: true });
    } finally {
      saving.delete(key);
      if (!disposed) sw.disabled = false;
    }
  }

  // ---- 1. Versione e stato
  function renderStatus() {
    const d = data;
    const c = d.check;
    const job = d.download_job;
    const p = d.pending;
    const out = [];

    const checkBtn = h('md-filled-tonal-button', { onclick: check }, icon('refresh', { slot: 'icon' }),
      busy === 'check' || d.checking ? 'Ricerca in corso…' : 'Cerca aggiornamenti');
    checkBtn.disabled = !d.configured || !!busy || d.checking;
    out.push(h('div', { class: 'upd-head' },
      h('div', { class: 'upd-version' },
        h('span', { class: 'upd-hint md-typescale-body-small' }, 'Versione installata'),
        h('span', { class: 'upd-version__num' }, d.current),
        h('span', { class: 'upd-hint md-typescale-body-small' },
          c?.checked_at ? `Ultimo controllo: ${formatDateTime(c.checked_at)}` : 'Nessun controllo eseguito')),
      h('div', { class: 'upd-actions' }, checkBtn)));

    if (!d.configured) {
      out.push(callout('info', 'info', 'Aggiornamenti non configurati',
        h('p', {}, 'Questa copia di FotoScan non indica da dove scaricare gli aggiornamenti. Nessuna connessione viene fatta.')));
      return out;
    }
    if (d.git_checkout) {
      out.push(callout('info', 'terminal', 'Copia scaricata con git',
        h('p', {}, 'Puoi cercare gli aggiornamenti, ma per installarli usa «git pull» nella cartella di FotoScan.')));
    }
    if (d.frozen && !d.exe_ready && !!d.check?.latest) {
      out.push(callout('info', 'download', 'Pacchetto Windows non ancora pubblicato',
        h('p', {}, 'Questa versione è disponibile ma la Release non include ancora PhotoScan.exe: scaricala dalla pagina Release.')));
    }
    if (d.last_install && d.last_install.to === d.current) {
      out.push(callout('ok', 'task_alt', `Aggiornato alla versione ${d.last_install.to}`,
        h('p', {}, `Installato il ${formatDateTime(d.last_install.at)} (prima: ${d.last_install.from}).`)));
    }

    if (job) {
      const prog = h('md-linear-progress', job.total ? { value: job.done / job.total } : { indeterminate: true });
      out.push(h('div', { class: 'upd-progress' },
        h('span', { class: 'md-typescale-body-large' }, 'Download dell’aggiornamento…'),
        prog,
        h('span', { class: 'upd-hint md-typescale-body-small' },
          job.total ? `${mb(job.done)} di ${mb(job.total)}` : `${mb(job.done)} scaricati`),
        h('div', { class: 'upd-actions' },
          h('md-text-button', { onclick: discard, disabled: !!busy }, icon('close', { slot: 'icon' }), 'Annulla'))));
    } else if (p) {
      const actions = [
        h('md-filled-button', { onclick: install, disabled: !!busy || d.git_checkout },
          icon('system_update_alt', { slot: 'icon' }), canRestart() ? 'Installa e riavvia' : 'Installa alla riapertura'),
        h('md-text-button', { onclick: discard, disabled: !!busy }, 'Annulla aggiornamento'),
      ];
      const when = p.approved || d.settings.update_auto_install
        ? 'Verrà installato alla prossima apertura di FotoScan.'
        : 'Scaricato e verificato: si installa quando lo decidi tu.';
      out.push(callout('warning', 'download_done', `FotoScan ${p.version} pronto da installare`, h('p', {}, when)));
      if (p.notes) out.push(h('pre', { class: 'upd-notes' }, p.notes));
      out.push(h('div', { class: 'upd-actions' }, actions));
    } else if (c?.available) {
      const l = c.latest;
      out.push(callout('warning', 'new_releases', `È disponibile FotoScan ${l.version}${l.prerelease ? ' (versione di prova)' : ''}`,
        h('p', {}, l.published_at ? `Pubblicata il ${formatDateTime(l.published_at)}.` : '')));
      if (l.notes) out.push(h('pre', { class: 'upd-notes' }, l.notes));
      out.push(h('div', { class: 'upd-actions' },
        h('md-filled-button', { onclick: download, disabled: !!busy || d.git_checkout || (d.frozen && !d.exe_ready) },
          icon('download', { slot: 'icon' }), busy === 'download' ? 'Avvio del download…' : 'Scarica'),
        l.html_url ? h('md-text-button', { href: l.html_url, target: '_blank', rel: 'noopener' },
          icon('open_in_new', { slot: 'icon' }), 'Novità su GitHub') : null));
    } else if (c?.error) {
      out.push(callout('error', 'cloud_off', 'Controllo non riuscito', h('p', {}, c.error)));
    } else if (c) {
      out.push(callout('ok', 'verified', 'FotoScan è aggiornato', h('p', {}, 'Stai usando l’ultima versione disponibile.')));
    }
    return out;
  }

  // ---- 2. Opzioni
  function renderOptions() {
    const rows = OPTIONS.map((o) => {
      const id = `upd-${o.key}`;
      const sw = document.createElement('md-switch');
      sw.selected = !!data.settings[o.key];
      sw.disabled = saving.has(o.key) || !data.configured;
      sw.setAttribute('aria-labelledby', id);
      sw.addEventListener('change', () => setOption(o.key, sw.selected, sw));
      return h('div', { class: 'upd-switch' },
        h('div', { class: 'upd-switch__text' },
          h('span', { id, class: 'md-typescale-body-large' }, o.title),
          h('span', { class: 'upd-hint md-typescale-body-medium' }, o.hint)),
        sw);
    });
    return [h('h2', { class: 'upd-group__title' }, 'Opzioni'), h('div', { class: 'upd-card' }, rows)];
  }

  function paint() {
    if (disposed || !data) return;
    statusSlot.replaceChildren(h('h2', { class: 'upd-group__title' }, 'Aggiornamenti'), h('div', { class: 'upd-card' }, renderStatus()));
    optionsSlot.replaceChildren(...renderOptions());
    if (!container.contains(statusSlot)) container.replaceChildren(statusSlot, optionsSlot);
  }

  const offUpdate = onEvent('update', (d) => { if (!disposed && d) { data = d; paint(); } });
  const offJob = onEvent('job', (j) => {
    if (disposed || !data || j?.name !== JOB) return;
    if (j.state === 'running') { data.download_job = j; paint(); return; }
    data.download_job = null;
    if (j.state === 'error' && !/annullato/i.test(j.error || '')) showSnackbar(j.error || 'Download non riuscito.', { error: true, duration: 6000 });
    load();
  });

  load();

  return {
    destroy() { disposed = true; offUpdate(); offJob(); },
    setServerErrors() {},
    reveal() {},
  };
}
