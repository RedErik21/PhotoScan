// Impostazioni > Rete: blocco della rete in uscita, connessioni previste e registro dei tentativi verso internet.
// Dati da GET /api/network (aggiornati ogni pochi secondi); POST /api/network/test prova il blocco;
// DELETE /api/network/log svuota il registro. Sezione di sola lettura: non usa la barra «Salva».
import { h, icon } from '../ui/el.js';
import { api } from '../api.js';
import { confirmDialog } from '../ui/dialog.js';
import { skeletonList } from '../ui/skeleton.js';
import { showSnackbar } from '../ui/snackbar.js';
import { formatDateTime, formatNumber } from '../ui/format.js';

const REFRESH_MS = 5000;
const STYLE_ID = 'fs-settings-network-style';
const CSS = `
.net-group { display: flex; flex-direction: column; gap: var(--sp-3); min-width: 0; }
.net-group__title {
  margin: 0; color: var(--md-sys-color-on-surface);
  font: 500 var(--md-sys-typescale-title-medium-size) / var(--md-sys-typescale-title-medium-line-height) var(--md-ref-typeface-plain);
}
.net-card {
  display: flex; flex-direction: column; gap: var(--sp-4); min-width: 0;
  padding: var(--sp-5) var(--sp-6); border-radius: var(--shape-lg);
  border: 1px solid var(--md-sys-color-outline-variant);
  background: var(--md-sys-color-surface-container); color: var(--md-sys-color-on-surface);
}
.net-hint { margin: 0; color: var(--md-sys-color-on-surface-variant); }
.net-info { display: grid; grid-template-columns: max-content minmax(0, 1fr); gap: var(--sp-2) var(--sp-4); margin: 0; }
.net-info dt { color: var(--md-sys-color-on-surface-variant); }
.net-info dd { margin: 0; min-width: 0; overflow-wrap: anywhere; }
.callout--ok { background: color-mix(in srgb, #2e7d32 16%, var(--md-sys-color-surface-container-high)); color: var(--md-sys-color-on-surface); }
.net-badge {
  display: inline-flex; align-items: center; gap: 4px; padding: 2px var(--sp-2); border-radius: var(--shape-full);
  white-space: nowrap; font-size: var(--md-sys-typescale-label-medium-size); font-weight: 500;
}
.net-badge--blocked { background: var(--md-sys-color-error-container); color: var(--md-sys-color-on-error-container); }
.net-badge--allowed { background: var(--md-sys-color-tertiary-container); color: var(--md-sys-color-on-tertiary-container); }
.net-badge--ok { background: var(--md-sys-color-primary-container); color: var(--md-sys-color-on-primary-container); }
.net-badge--out { background: var(--md-sys-color-surface-container-highest); color: var(--md-sys-color-on-surface-variant); }
.net-list { display: flex; flex-direction: column; min-width: 0; }
.net-dest { display: flex; flex-direction: column; gap: var(--sp-1); padding: var(--sp-3) 0; border-top: 1px solid var(--md-sys-color-outline-variant); }
.net-dest:first-child { border-top: 0; padding-top: 0; }
.net-dest__head { display: flex; flex-wrap: wrap; align-items: center; gap: var(--sp-2); }
.net-dest__name { font-weight: 500; }
.net-sub { color: var(--md-sys-color-on-surface-variant); }
.net-mono { font-family: var(--md-ref-typeface-mono, ui-monospace, Consolas, monospace); font-size: 0.92em; overflow-wrap: anywhere; }
.net-stats { display: flex; flex-wrap: wrap; gap: var(--sp-3); }
.net-stat {
  flex: 1 1 140px; display: flex; flex-direction: column; gap: 2px; padding: var(--sp-3) var(--sp-4);
  border-radius: var(--shape-md); background: var(--md-sys-color-surface-container-high);
}
.net-stat__value { font-size: var(--md-sys-typescale-headline-small-size); font-variant-numeric: tabular-nums; }
.net-actions { display: flex; flex-wrap: wrap; align-items: center; gap: var(--sp-2); }
.net-filter { display: flex; flex-wrap: wrap; gap: var(--sp-2); }
.net-row {
  display: grid; grid-template-columns: 150px 110px minmax(0, 1fr); gap: var(--sp-1) var(--sp-4); align-items: start;
  padding: var(--sp-3) 0; border-top: 1px solid var(--md-sys-color-outline-variant);
}
.net-row--head { border-top: 0; padding-top: 0; color: var(--md-sys-color-on-surface-variant); }
.net-row__what { display: flex; flex-direction: column; gap: 2px; min-width: 0; }
.net-row__time { font-variant-numeric: tabular-nums; color: var(--md-sys-color-on-surface-variant); }
.net-empty { padding: var(--sp-4) 0; color: var(--md-sys-color-on-surface-variant); }
.net-cmd {
  margin: 0; padding: var(--sp-3); border-radius: var(--shape-sm); overflow-x: auto; white-space: pre;
  background: var(--md-sys-color-surface-container-highest); font-family: ui-monospace, Consolas, monospace; font-size: 0.85em;
}
@media (max-width: 699px) {
  .net-row { grid-template-columns: minmax(0, 1fr) auto; }
  .net-row__what { grid-column: 1 / -1; }
  .net-row--head { display: none; }
}
`;

function injectStyle() {
  if (document.getElementById(STYLE_ID)) return;
  const style = document.createElement('style');
  style.id = STYLE_ID;
  style.textContent = CSS;
  document.head.append(style);
}

const group = (title, ...children) => h('section', { class: 'net-group' },
  h('h2', { class: 'net-group__title' }, title),
  h('div', { class: 'net-card' }, ...children));
const hint = (text) => h('p', { class: 'net-hint md-typescale-body-medium' }, text);
const badge = (kind, text, ic) => h('span', { class: `net-badge net-badge--${kind}` },
  ic ? h('span', { class: 'material-symbols-outlined', style: { fontSize: '16px' }, 'aria-hidden': 'true' }, ic) : null, text);
const callout = (kind, ic, title, ...body) => h('div', { class: `callout callout--${kind}`, role: 'status' },
  h('span', { class: 'callout__icon material-symbols-outlined', 'aria-hidden': 'true' }, ic),
  h('div', {}, h('p', { class: 'callout__title' }, title), ...body));

export async function mountSection(container) {
  injectStyle();
  let disposed = false;
  let data = null;
  let filter = 'all';
  let timer = null;
  let testing = false;

  const statusSlot = h('div', { class: 'net-group' });
  const expectedSlot = h('div', { class: 'net-group' });
  const logSlot = h('div', { class: 'net-group' });
  container.replaceChildren(skeletonList(4));

  /** Con la finestra nascosta il registro non si rilegge: si riprende al ritorno in primo piano. */
  function reloadWhenVisible() {
    if (disposed) return;
    if (!document.hidden) { load({ quiet: true }); return; }
    document.addEventListener('visibilitychange', function again() {
      if (document.hidden) return;
      document.removeEventListener('visibilitychange', again);
      if (!disposed) load({ quiet: true });
    });
  }

  async function load({ quiet = false } = {}) {
    try {
      const d = await api.get('/network', { limit: 300 });
      if (disposed) return;
      data = d;
      paint();
    } catch (e) {
      if (disposed || quiet) return;
      container.replaceChildren(hint(`Impossibile leggere lo stato della rete: ${e.message}`));
    } finally {
      if (!disposed) { clearTimeout(timer); timer = setTimeout(reloadWhenVisible, REFRESH_MS); }
    }
  }

  // ---- 1. Protezione
  function renderStatus() {
    const g = data.guard;
    const s = data.server;
    const tele = Object.values(g.telemetry_env || {}).every((v) => v === '1');
    const head = g.installed
      ? (g.window_open
        ? callout('warning', 'cloud_download', 'Download di un modello in corso',
          h('p', {}, `Rete aperta temporaneamente solo verso ${g.download_hosts.join(', ')} per: ${g.window_reason || 'download'}. Si richiude da sola al termine.`))
        : callout('ok', 'shield_lock', 'Rete bloccata: PhotoScan non comunica con internet',
          h('p', {}, 'Ogni connessione verso l\'esterno viene rifiutata. Sono permesse solo le comunicazioni interne a questo computer (127.0.0.1), che servono alla finestra dell\'app per parlare con il programma.')))
      : callout('error', 'gpp_bad', 'Blocco della rete NON attivo',
        h('p', {}, 'Riavvia PhotoScan. Se il problema resta, controlla il file fotoscan_ui.log.'));

    const testBtn = h('md-filled-tonal-button', { onclick: runTest }, icon('network_check', { slot: 'icon' }), 'Prova il blocco');
    testBtn.disabled = testing;

    return [
      h('h2', { class: 'net-group__title' }, 'Protezione'),
      h('div', { class: 'net-card' },
        head,
        h('dl', { class: 'net-info md-typescale-body-medium' },
          h('dt', {}, 'Blocco connessioni'), h('dd', {}, g.installed ? 'Attivo (in tutti i processi di PhotoScan)' : 'Non attivo'),
          h('dt', {}, 'Programma interno'), h('dd', {},
            s.host
              ? `In ascolto su ${s.host}:${s.port ?? '—'} — ${s.local_only ? 'raggiungibile solo da questo computer' : 'ATTENZIONE: raggiungibile da altri dispositivi della rete'}`
              : `Porta ${s.port ?? '—'}`),
          h('dt', {}, 'Modelli di intelligenza artificiale'), h('dd', {}, 'Letti solo dal computer. La rete si apre solo se un modello manca, e solo per scaricarlo.'),
          h('dt', {}, 'Download consentiti verso'), h('dd', { class: 'net-mono' }, g.download_hosts.join(', ')),
          h('dt', {}, 'Telemetria delle librerie'), h('dd', {}, tele ? 'Disattivata (Hugging Face, onnxruntime)' : 'Non del tutto disattivata'),
          h('dt', {}, 'Registro'), h('dd', { class: 'net-mono' }, g.log_file)),
        h('div', { class: 'net-actions' }, testBtn,
          h('span', { class: 'net-sub md-typescale-body-small' }, 'Prova a raggiungere example.com e verifica che venga fermato.'))),
    ];
  }

  async function runTest() {
    if (testing) return;
    testing = true;
    paint();
    try {
      const r = await api.post('/network/test', {});
      showSnackbar(r.message, { error: !r.blocked, duration: 6000 });
    } catch (e) {
      showSnackbar(e.message, { error: true });
    } finally {
      testing = false;
      if (!disposed) load();
    }
  }

  // ---- 2. Connessioni previste
  function renderExpected() {
    const rows = data.expected.map((x) => h('div', { class: 'net-dest' },
      h('div', { class: 'net-dest__head' },
        h('span', { class: 'net-dest__name' }, x.name),
        h('span', { class: 'net-sub net-mono md-typescale-body-small' }, x.hosts.join(', ')),
        x.managed
          ? badge(x.ok ? 'ok' : 'allowed', x.ok ? 'Nessuna connessione' : 'Download una tantum', x.ok ? 'check' : 'download')
          : badge('out', 'Fuori da PhotoScan', 'open_in_new')),
      h('div', { class: 'md-typescale-body-medium' }, x.purpose),
      h('div', { class: 'net-sub md-typescale-body-small' }, `Quando: ${x.when}`),
      h('div', { class: 'net-sub md-typescale-body-small' }, `Cosa parte dal computer: ${x.sends}`),
      h('div', { class: 'net-sub md-typescale-body-small' }, x.state)));
    return [
      h('h2', { class: 'net-group__title' }, 'Connessioni previste'),
      h('div', { class: 'net-card' },
        hint('Queste sono le uniche destinazioni esterne conosciute. Le foto, le miniature, i volti e il catalogo non vengono mai inviati.'),
        h('div', { class: 'net-list' }, rows)),
    ];
  }

  // ---- 3. Tentativi di connessione
  function renderLog() {
    const sum = data.summary;
    const events = data.events.filter((e) => filter === 'all'
      || (filter === 'blocked' && e.outcome === 'bloccato') || (filter === 'allowed' && e.outcome === 'consentito'));
    const chip = (id, label) => {
      const c = h('md-filter-chip', { label, onclick: () => { filter = id; paint(); } });
      c.selected = filter === id;
      return c;
    };
    const rows = events.length
      ? events.map((e) => h('div', { class: 'net-row md-typescale-body-medium' },
        h('span', { class: 'net-row__time' }, formatDateTime(e.ts)),
        h('span', {}, e.outcome === 'bloccato' ? badge('blocked', 'Bloccato', 'block') : badge('allowed', 'Consentito', 'check')),
        h('div', { class: 'net-row__what' },
          h('span', {}, e.purpose || 'Origine non riconosciuta'),
          h('span', { class: 'net-mono net-sub' }, `${e.host || '?'}${e.port ? ':' + e.port : ''}`),
          h('span', { class: 'net-sub md-typescale-body-small' },
            [e.caller && `da ${e.caller}`, e.library && e.library !== (e.caller || '').split(':')[0] && `tramite ${e.library}`,
              e.process && `processo: ${e.process}`].filter(Boolean).join(' · ')))))
      : [h('div', { class: 'net-empty md-typescale-body-medium' },
        data.events.length ? 'Nessun tentativo con questo filtro.' : 'Nessun tentativo di connessione verso internet registrato.')];

    return [
      h('h2', { class: 'net-group__title' }, 'Tentativi di connessione verso internet'),
      h('div', { class: 'net-card' },
        hint('Ogni volta che una parte di PhotoScan prova a uscire su internet viene annotato qui: cosa, verso dove e se è stato fermato. Le comunicazioni interne al computer non sono elencate.'),
        h('div', { class: 'net-stats' },
          h('div', { class: 'net-stat' }, h('span', { class: 'net-sub md-typescale-body-small' }, 'Bloccati'),
            h('span', { class: 'net-stat__value' }, formatNumber(sum.blocked))),
          h('div', { class: 'net-stat' }, h('span', { class: 'net-sub md-typescale-body-small' }, 'Consentiti (download modelli)'),
            h('span', { class: 'net-stat__value' }, formatNumber(sum.allowed))),
          h('div', { class: 'net-stat' }, h('span', { class: 'net-sub md-typescale-body-small' }, 'Ultimo tentativo'),
            h('span', { class: 'md-typescale-body-large' }, sum.last ? formatDateTime(sum.last) : 'mai'))),
        h('div', { class: 'net-actions' },
          h('div', { class: 'net-filter' }, chip('all', 'Tutti'), chip('blocked', 'Bloccati'), chip('allowed', 'Consentiti')),
          h('span', { style: { flex: '1' } }),
          h('md-text-button', { onclick: () => load() }, icon('refresh', { slot: 'icon' }), 'Aggiorna'),
          h('md-text-button', { onclick: clearLog, disabled: !data.events.length }, icon('delete_sweep', { slot: 'icon' }), 'Svuota registro')),
        h('div', { class: 'net-list', role: 'table', 'aria-label': 'Tentativi di connessione' },
          h('div', { class: 'net-row net-row--head md-typescale-label-large', role: 'row' },
            h('span', {}, 'Quando'), h('span', {}, 'Esito'), h('span', {}, 'Per cosa / destinazione')),
          rows)),
    ];
  }

  async function clearLog() {
    const ok = await confirmDialog({
      title: 'Svuotare il registro?',
      message: 'Vengono cancellate solo le righe del registro dei tentativi di connessione. Il blocco resta attivo.',
      confirmLabel: 'Svuota',
    });
    if (!ok || disposed) return;
    try {
      await api.del('/network/log');
      showSnackbar('Registro svuotato.');
    } catch (e) {
      showSnackbar(e.message, { error: true });
    }
    load();
  }

  // ---- 4. Fuori da PhotoScan
  function renderOutside() {
    const cmd = [
      '# PowerShell come amministratore: blocca qualsiasi uscita del Python di PhotoScan',
      'New-NetFirewallRule -DisplayName "PhotoScan blocca uscita" -Direction Outbound -Action Block `',
      '  -Program "<cartella PhotoScan>\\.venv\\Scripts\\python.exe"',
    ].join('\n');
    return group('Per una garanzia anche a livello di sistema',
      hint('Il blocco di PhotoScan copre tutto il codice Python dell\'app e delle librerie di intelligenza artificiale. Non copre il browser (Edge/WebView2), i link che apri tu (OpenStreetMap) e Windows. Per bloccare tutto a livello di sistema puoi aggiungere una regola nel firewall di Windows dopo aver scaricato i modelli:'),
      h('pre', { class: 'net-cmd' }, cmd),
      hint('Nota: in alcuni ambienti Python il programma vero gira con il python.exe di sistema; controlla in Gestione attività quale processo usare.'));
  }

  function paint() {
    if (disposed || !data) return;
    statusSlot.replaceChildren(...renderStatus());
    expectedSlot.replaceChildren(...renderExpected());
    logSlot.replaceChildren(...renderLog());
    if (!container.contains(statusSlot)) {
      container.replaceChildren(statusSlot, logSlot, expectedSlot, renderOutside());
    }
  }

  load();

  return {
    destroy() { disposed = true; clearTimeout(timer); },
    setServerErrors() {},
    reveal() {},
  };
}
