// Importa dal telefono: sfoglia le cartelle del telefono (Android via MTP, iPhone solo DCIM), scegli quelle con foto/video
// e copiale in una cartella di destinazione. Il telefono resta in sola lettura; la copia gira sul server
// come job (phone_import) e l'avanzamento arriva via SSE. Non modifica la cartella delle foto originali.
// Aggiornamenti: l'SSE e le scelte aggiornano solo la loro sezione (niente ricostruzione di tutta la pagina),
// cosi' il pulsante "Annulla" e il campo destinazione non perdono il clic o il focus.
import { h, icon } from '../ui/el.js';
import { api, ApiError, onEvent } from '../api.js';
import { choiceDialog, confirmDialog } from '../ui/dialog.js';
import { showSnackbar } from '../ui/snackbar.js';
import { errorState, emptyState } from '../ui/empty-state.js';
import { formatDateTime, formatEta, formatNumber, plural } from '../ui/format.js';

const DEST_KEY = 'fotoscan.phone.dest';
const JOB = 'phone_import';

// Stato della view: resta tra una navigazione e l'altra, il job continua sul server.
const S = {
  devices: null,        // [{name, storages:[...]}] oppure null se non ancora caricati
  device: '', storage: '',
  path: [],             // cartella corrente (array di nomi)
  view: null,           // risposta di GET /phone/folder per path corrente
  selected: new Map(),  // chiave JSON(path) -> path, cartelle scelte (mai una cartella e una sua sottocartella insieme)
  dest: localStorage.getItem(DEST_KEY) || '',
  jobId: null, job: null, result: null,
  cancelling: false,
  busy: '',             // '' | 'devices' | 'folder'
  scanning: false,      // anteprima in corso (POST /phone/scan)
  history: null,        // storico importazioni (GET /phone/history)
};

// Riferimenti alla pagina corrente (ricreata da render()); le sezioni si aggiornano in place.
let UI = { root: null, ctx: null };

let cssLoaded = false;
function loadCss() {
  if (cssLoaded || document.querySelector('link[data-view="phone"]')) { cssLoaded = true; return; }
  cssLoaded = true;
  const l = document.createElement('link');
  l.rel = 'stylesheet'; l.href = 'css/view-phone.css'; l.dataset.view = 'phone';
  document.head.append(l);
}

function errText(e) {
  if (e instanceof ApiError) return e.message;
  return e?.message || 'Operazione non riuscita.';
}

const keyOf = (p) => JSON.stringify(p);
/** true se a e' uguale a b o ne e' un antenato (a contiene b). */
const contains = (a, b) => a.length <= b.length && a.every((x, i) => x === b[i]);

/** Cartella scelta che contiene gia' `path` (antenato), oppure null. */
function coveringSelection(path) {
  return [...S.selected.values()].find((p) => p.length < path.length && contains(p, path)) || null;
}

/** Seleziona/deseleziona una cartella mantenendo la regola: niente cartella e sua sottocartella insieme. */
function setSelected(path, on) {
  const k = keyOf(path);
  if (!on) { S.selected.delete(k); return; }
  for (const [kk, p] of S.selected) if (p.length > path.length && contains(path, p)) S.selected.delete(kk);
  S.selected.set(k, path);
}

// ---------------------------------------------------------------- dati
async function loadDevices() {
  const { ctx } = UI;
  S.error = null; S.busy = 'devices';
  renderDevices();
  try {
    const r = await api.get('/phone/devices', null, ctx.signal);
    S.devices = r.devices || [];
    if (!S.devices.length) { S.device = ''; S.storage = ''; S.selected.clear(); }
    else {
      if (!S.devices.some((d) => d.name === S.device)) S.device = S.devices[0].name;
      const dev = S.devices.find((d) => d.name === S.device);
      if (!dev.storages.includes(S.storage)) S.storage = dev.storages[0] || '';
      S.path = [];
    }
  } catch (e) {
    if (e.name === 'AbortError') { S.busy = ''; return; }
    S.devices = null;
    S.error = errText(e);
  }
  S.busy = '';
  render();
  if (S.devices?.length && S.storage) await loadFolder();
}

async function loadFolder() {
  const { ctx } = UI;
  if (!S.device || !S.storage) { renderList(); return; }
  S.view = null; S.error = null; S.busy = 'folder';
  renderList();
  let aborted = false;
  try {
    const params = new URLSearchParams({ device: S.device, storage: S.storage });
    for (const p of S.path) params.append('path', p);
    S.view = await api.get(`/phone/folder?${params}`, null, ctx.signal);
  } catch (e) {
    if (e.name === 'AbortError') aborted = true;
    else S.error = errText(e);
  }
  S.busy = '';
  if (!aborted) renderList();
}

async function loadHistory(ctx) {
  try {
    const r = await api.get('/phone/history', null, ctx?.signal);
    S.history = r.items || [];
  } catch (e) {
    if (e.name !== 'AbortError') S.history = S.history || [];
  }
}

// ---------------------------------------------------------------- interfaccia: pezzi
function busyLine(text) {
  return h('div', { class: 'phone-busy', role: 'status' },
    h('md-circular-progress', { indeterminate: true }),
    h('span', {}, text));
}

function header() {
  return h('div', { class: 'view-header' },
    h('div', {},
      h('h1', { class: 'md-typescale-headline-medium' }, 'Importa dal telefono'),
      h('p', { class: 'muted md-typescale-body-medium' }, 'Copia foto e video da Android o iPhone sul tuo PC in sola lettura: nulla viene mai cancellato dal telefono.')),
    h('div', { class: 'view-header-actions' },
      h('span', { class: 'phone-head-pill' }, icon('verified_user'), 'Sola lettura · USB')));
}

function crumbs() {
  const items = [h('button', { class: 'phone-crumb', type: 'button', onclick: () => { S.path = []; loadFolder(); } }, S.storage || 'Memoria')];
  S.path.forEach((name, i) => {
    items.push(h('span', { class: 'phone-crumb-sep', 'aria-hidden': 'true' }, '/'));
    items.push(h('button', { class: 'phone-crumb', type: 'button',
      onclick: () => { S.path = S.path.slice(0, i + 1); loadFolder(); } }, name));
  });
  return h('nav', { class: 'phone-crumbs', 'aria-label': 'Percorso nel telefono' }, ...items);
}

/** Riga di una cartella. Tre casi: scelta non possibile (nulla da importare o gia' inclusa in una scelta). */
function folderRow(f) {
  const path = [...S.path, f.name];
  const k = keyOf(path);
  const anc = coveringSelection(path);
  const empty = !f.sub && !f.media;
  const cb = h('md-checkbox', {
    checked: S.selected.has(k) || !!anc || undefined,
    disabled: !!anc || empty || undefined,
    'aria-label': empty ? `${f.name}: nessun file da importare` : `Seleziona ${f.name}`,
    dataset: { key: k },
  });
  cb.addEventListener('change', () => {
    setSelected(path, cb.checked);
    renderSelection();
    renderList(k);
  });
  const isSel = S.selected.has(k) || !!anc;
  const countText = anc ? `Inclusa in «${anc[anc.length - 1]}»`
    : empty ? 'Vuota'
      : f.media ? `${plural(f.media, 'file', 'file')}`
        : 'Sottocartelle';
  return h('li', { class: `phone-row${isSel ? ' is-selected' : ''}` },
    cb,
    h('button', { class: 'phone-row__name', type: 'button', onclick: () => {
      if (!f.sub) { if (!cb.disabled) cb.click(); return; }
      S.path = path; loadFolder();
    } },
      h('span', { class: `phone-row__icon${empty ? ' phone-row__icon--empty' : ''}`, 'aria-hidden': 'true' },
        icon(f.sub ? 'folder' : f.media ? 'photo_library' : 'folder_off')),
      h('span', { class: 'phone-row__title' }, f.name),
      h('span', { class: 'phone-row__count' }, countText)),
    f.sub ? h('md-icon-button', { 'aria-label': `Apri ${f.name}`, onclick: () => { S.path = path; loadFolder(); } },
      icon('chevron_right')) : null);
}

function selectedSummary() {
  if (!S.selected.size) return h('p', { class: 'muted md-typescale-body-medium' }, 'Spunta una o più cartelle nell’elenco a sinistra.');
  return h('div', { class: 'phone-selected' },
    h('div', { class: 'phone-selected__chips' },
      ...[...S.selected.entries()].map(([k, p]) => h('span', { class: 'phone-sel-chip', title: p.join(' / ') },
        h('span', { class: 'phone-sel-chip__path' }, p.join(' / ')),
        h('button', {
          type: 'button', class: 'phone-sel-chip__rm', 'aria-label': `Rimuovi ${p.join(' / ')}`,
          onclick: () => { S.selected.delete(k); renderSelection(); renderList(); },
        }, icon('close'))))),
    h('div', { class: 'phone-actions' },
      h('md-text-button', { onclick: () => { S.selected.clear(); renderSelection(); renderList(); } }, 'Deseleziona tutte')));
}

function destField() {
  const field = h('md-outlined-text-field', { label: 'Cartella di destinazione', value: S.dest, class: 'phone-dest',
    'supporting-text': 'Dove salvare le foto copiate dal telefono', style: 'flex:1; min-width:0' });
  field.addEventListener('input', () => { S.dest = field.value; localStorage.setItem(DEST_KEY, S.dest); updateActions(); });
  const pick = h('md-outlined-button', { onclick: async () => {
    try {
      const r = await api.post('/system/pick-folder', { title: 'Scegli dove copiare le foto dal telefono', initial: S.dest.trim() || undefined });
      if (r && r.path) { S.dest = r.path; localStorage.setItem(DEST_KEY, S.dest); renderSelection(); }
      else if (r && r.available === false) showSnackbar('Scelta cartella non disponibile: scrivi il percorso a mano.', { error: true });
    } catch (e) { showSnackbar(errText(e), { error: true }); }
  } }, icon('folder_open', { slot: 'icon' }), 'Scegli…');
  return h('div', { class: 'phone-dest-row' }, field, pick);
}

/** Stato del pulsante Importa e spiegazione sotto di esso (perche' non si puo' ancora premere). */
function actionState() {
  if (S.scanning) return { disabled: true, hint: 'Controllo i file sul telefono: può richiedere alcuni minuti. Non scollegarlo.' };
  if (S.job?.state === 'running') return { disabled: true, hint: 'Copia in corso: l’avanzamento è in alto nella pagina.' };
  if (!S.selected.size) return { disabled: true, hint: 'Scegli almeno una cartella nell’elenco qui sotto.' };
  if (!S.dest.trim()) return { disabled: true, hint: 'Scegli la cartella di destinazione: dove salvare le foto.' };
  return { disabled: false, hint: '' };
}

function updateActions() {
  if (!UI.importBtn) return;
  const a = actionState();
  UI.importBtn.disabled = a.disabled;
  UI.hint.textContent = a.hint;
  UI.hint.hidden = !a.hint;
  UI.scanBar.hidden = !S.scanning;
}

/** Tempi stimati dalla frazione completata: restano, residuo e totale. Nulla finche' non c'e' un avanzamento. */
function timing(j) {
  const frac = j.total ? j.done / j.total : 0;
  if (!(frac > 0) || !(j.elapsed > 0)) return null;
  const total = j.elapsed / frac;
  return { left: Math.max(0, total - j.elapsed), total };
}

function nextStep() {
  return h('div', { class: 'callout callout--info phone-next', role: 'status' },
    icon('neurology'),
    h('div', { class: 'phone-next__body' },
      h('h2', { class: 'callout__title md-typescale-title-medium' }, 'Prossimo passo: Analisi'),
      h('p', {}, 'Le nuove foto vanno analizzate per trovare date e soggetti.'),
      h('div', { class: 'phone-next__actions' },
        h('md-filled-tonal-button', { onclick: () => UI.ctx?.navigate?.('/index') }, 'Apri Analisi'))));
}

function resultBlock(r, state) {
  return h('div', { class: 'phone-result' },
    h('p', {}, `${plural(r.new_files, 'file nuovo copiato', 'file nuovi copiati')} in `, h('code', {}, r.dest)),
    r.skipped_known ? h('p', { class: 'muted md-typescale-body-medium' }, `${plural(r.skipped_known, 'già presente non copiato (nome e dimensione uguali)', 'già presenti non copiati (nome e dimensione uguali)')}.`) : null,
    r.skipped_existing ? h('p', { class: 'muted md-typescale-body-medium' }, `${plural(r.skipped_existing, 'già presente (saltato)', 'già presenti (saltati)')}.`) : null,
    r.errors?.length ? h('details', { class: 'phone-errors' },
      h('summary', {}, `${plural(r.errors.length, 'file non copiato', 'file non copiati')}`),
      h('ul', {}, ...r.errors.slice(0, 200).map((e) => h('li', {}, e)))) : null,
    state === 'done' && r.new_files ? nextStep() : null);
}

// ---- card di avanzamento: struttura ricreata solo al cambio di stato; i valori si aggiornano in place
let jobKey = null;
let jobRefs = null;

function buildJobCard() {
  const j = S.job;
  const running = j.state === 'running';
  jobRefs = null;
  if (running) {
    const bar = h('md-linear-progress', {});
    const pct = h('strong', {});
    const eta = h('span', {});
    const tot = h('span', {});
    const msg = h('p', { class: 'muted md-typescale-body-medium phone-msg' });
    jobRefs = { bar, pct, eta, tot, msg };
    return h('section', { class: 'card phone-job' },
      h('h2', { class: 'section-title md-typescale-title-medium phone-h2', 'aria-live': 'polite' }, S.cancelling ? 'Annullamento in corso…' : 'Copia in corso'),
      bar,
      h('div', { class: 'phone-progress-meta' }, pct, eta, tot),
      msg,
      h('div', { class: 'phone-actions' },
        h('md-text-button', { disabled: S.cancelling || undefined, onclick: cancelJob }, 'Annulla copia')));
  }
  const title = j.state === 'done' ? 'Copia completata' : j.state === 'cancelled' ? 'Copia annullata' : 'Copia non riuscita';
  return h('section', { class: 'card phone-job' },
    h('h2', { class: 'section-title md-typescale-title-medium phone-h2', 'aria-live': 'polite' }, title),
    j.error ? h('p', { class: 'muted md-typescale-body-medium' }, j.error) : null,
    S.result ? resultBlock(S.result, j.state) : null,
    h('div', { class: 'phone-actions' },
      h('md-text-button', { onclick: closeJob }, 'Chiudi')));
}

function updateJobProgress() {
  if (!jobRefs || !S.job) return;
  const j = S.job;
  const t = timing(j);
  const pct = j.total ? Math.min(100, Math.round((j.done / j.total) * 100)) : null;
  jobRefs.bar.indeterminate = !j.total;
  jobRefs.bar.value = j.total ? j.done / j.total : 0;
  jobRefs.pct.textContent = pct !== null ? `${pct}%` : 'Preparazione…';
  jobRefs.eta.textContent = t ? `Restano circa ${formatEta(t.left)}` : 'Calcolo del tempo…';
  jobRefs.tot.textContent = t ? `Totale stimato ${formatEta(t.total)}` : '';
  jobRefs.msg.textContent = j.message || 'Preparazione dal telefono…';
}

function renderJob() {
  const box = UI.jobSlot;
  if (!box) return;
  if (!S.job) { box.replaceChildren(); jobKey = null; return; }
  const key = `${S.job.id}|${S.job.state}|${S.cancelling}|${S.result ? 1 : 0}`;
  if (key !== jobKey) { jobKey = key; box.replaceChildren(buildJobCard()); }
  updateJobProgress();
}

function cancelJob() {
  const j = S.job;
  if (!j || S.cancelling) return;
  S.cancelling = true; renderJob();
  api.post(`/jobs/${j.id}/cancel`, {}).catch(() => {
    S.cancelling = false; renderJob();
    showSnackbar('Non sono riuscito ad annullare la copia. Riprova.', { error: true });
  });
}

function closeJob() {
  S.job = null; S.jobId = null; S.result = null; S.cancelling = false;
  renderJob(); updateActions();
}

// ---- sezioni
function renderDevices() {
  const box = UI.devSlot;
  if (!box) return;
  const busy = S.busy === 'devices';
  const kids = [];
  if (S.devices === null) {
    kids.push(h('section', { class: 'card phone-connect' },
      h('div', { class: 'phone-connect__top' },
        h('div', { class: 'phone-connect__lead' },
          h('div', { class: 'phone-connect__icon', 'aria-hidden': 'true' }, icon('smartphone')),
          h('div', { class: 'phone-connect__copy' },
            h('h2', { class: 'section-title md-typescale-title-medium phone-h2' }, 'Collega lo smartphone via USB'),
            h('p', {}, 'Scegli le cartelle da copiare: i file già importati vengono saltati automaticamente.'))),
        h('md-filled-button', { disabled: busy || undefined, onclick: () => { S.error = null; loadDevices(); } },
          icon('phonelink_setup', { slot: 'icon' }), 'Cerca telefono')),
      h('div', { class: 'phone-os-grid' },
        h('div', { class: 'phone-os-card' },
          h('div', { class: 'phone-os-card__head' },
            h('span', { class: 'phone-os-card__badge', 'aria-hidden': 'true' }, icon('android')),
            h('span', {}, 'Android'),
            h('span', { class: 'phone-os-card__tag' }, 'MTP')),
          h('ol', { class: 'phone-connect__steps' },
            h('li', {}, 'Collega il cavo USB e sblocca lo schermo.'),
            h('li', {}, 'Nella notifica USB scegli ', h('strong', {}, '«Trasferimento file»'), '.'),
            h('li', {}, 'Premi ', h('strong', {}, 'Cerca telefono'), ' per sfogliare tutte le cartelle.'))),
        h('div', { class: 'phone-os-card' },
          h('div', { class: 'phone-os-card__head' },
            h('span', { class: 'phone-os-card__badge', 'aria-hidden': 'true' }, icon('phone_iphone')),
            h('span', {}, 'iPhone'),
            h('span', { class: 'phone-os-card__tag' }, 'DCIM / HEIC')),
          h('ol', { class: 'phone-connect__steps' },
            h('li', {}, 'Collega il cavo USB e sblocca iPhone.'),
            h('li', {}, 'Tocca ', h('strong', {}, '«Consenti»'), ' e ', h('strong', {}, '«Considera attendibile»'), '.'),
            h('li', {}, 'Premi ', h('strong', {}, 'Cerca telefono'), ' per importare da DCIM.')))),
      busy ? busyLine('Cerco il telefono collegato… può volerci qualche secondo.') : null,
      !busy && S.error ? errorState({ title: 'Telefono non raggiungibile', message: S.error, onRetry: () => loadDevices() }) : null));
  } else if (!S.devices.length) {
    kids.push(busy ? busyLine('Cerco il telefono collegato…') : h('section', { class: 'card phone-empty-card' },
      emptyState({ icon: 'smartphone', title: 'Nessun telefono trovato',
        message: 'Collega il telefono con il cavo USB e sbloccalo. Android: scegli «Trasferimento file» (MTP) nella notifica USB. iPhone: tocca «Consenti». Poi riprova.',
        actionLabel: 'Riprova', onAction: () => loadDevices() })));
  } else {
    const activeDev = S.devices.find((d) => d.name === S.device);
    const isApple = !!activeDev?.apple;
    kids.push(h('section', { class: 'card phone-device-bar' },
      h('div', { class: 'phone-device-bar__head' },
        h('span', { class: 'phone-status-pill' },
          h('span', { class: 'phone-status-dot', 'aria-hidden': 'true' }),
          'Dispositivo collegato'),
        h('span', { class: 'phone-os-chip' },
          icon(isApple ? 'phone_iphone' : 'android'),
          isApple ? 'iPhone (DCIM)' : 'Android (MTP)')),
      h('div', { class: 'phone-pick' },
        h('md-outlined-select', { label: 'Telefono', 'aria-label': 'Telefono', onchange: (e) => {
          S.device = e.target.value; S.storage = S.devices.find((d) => d.name === S.device)?.storages[0] || ''; S.path = []; S.selected.clear();
          renderSelection(); loadFolder();
        } }, ...S.devices.map((d) => h('md-select-option', { value: d.name, selected: d.name === S.device ? true : undefined }, h('div', { slot: 'headline' }, d.name)))),
        h('md-outlined-select', { label: 'Memoria', 'aria-label': 'Memoria', onchange: (e) => {
          S.storage = e.target.value; S.path = []; S.selected.clear(); renderSelection(); loadFolder();
        } },
          ...(activeDev?.storages || []).map((s) => h('md-select-option', { value: s, selected: s === S.storage ? true : undefined }, h('div', { slot: 'headline' }, s)))),
        h('md-icon-button', { 'aria-label': 'Cerca di nuovo il telefono', title: 'Cerca di nuovo', disabled: busy || undefined,
          onclick: () => loadDevices() }, icon('refresh'))),
      isApple ? h('div', { class: 'callout callout--info phone-apple-note' },
        icon('info'),
        h('p', {}, 'iPhone espone la cartella DCIM (fotocamera). Per gli originali HEIC/MOV: Impostazioni › Foto › Trasferisci su Mac o PC › Conserva originali. Su Windows serve l’app Dispositivi Apple (o iTunes).')) : null,
      busy ? busyLine('Cerco il telefono collegato…') : null,
      !S.storage ? emptyState({ icon: 'lock', title: 'Memoria del telefono non disponibile',
        message: 'Il telefono è collegato ma non mostra la memoria. Sbloccalo e lascia lo schermo acceso. Android: scegli «Trasferimento file» (MTP) nella notifica USB. iPhone: tocca «Consenti». Poi premi Riprova.',
        actionLabel: 'Riprova', onAction: () => loadDevices() }) : null));
  }
  box.replaceChildren(...kids);
}

function renderSelection() {
  const box = UI.selSlot;
  if (!box) return;
  if (!S.devices?.length || !S.storage) { box.replaceChildren(); UI.importBtn = null; return; }
  UI.importBtn = h('md-filled-button', { class: 'phone-import-btn', onclick: () => startImport() }, icon('file_download', { slot: 'icon' }), 'Importa foto e video');
  UI.hint = h('p', { class: 'muted md-typescale-body-medium phone-hint', 'aria-live': 'polite' });
  UI.scanBar = h('md-linear-progress', { indeterminate: true, hidden: true });
  box.replaceChildren(h('section', { class: 'card phone-selection' },
    h('div', { class: 'phone-selection__head' },
      h('h2', { class: 'section-title md-typescale-title-medium phone-h2' }, 'Cosa importare'),
      S.selected.size ? h('span', { class: 'phone-selection__count' }, plural(S.selected.size, 'cartella', 'cartelle')) : null),
    selectedSummary(),
    destField(),
    h('div', { class: 'phone-actions' }, UI.importBtn),
    UI.hint,
    UI.scanBar));
  updateActions();
}

function renderList(focusKey) {
  const box = UI.listSlot;
  if (!box) return;
  if (!S.devices?.length || !S.storage) { box.replaceChildren(); return; }
  let metaText = '';
  let content;
  if (S.error) {
    content = h('div', { class: 'phone-browser__empty' }, errorState({ title: 'Cartella non letta', message: S.error, onRetry: () => loadFolder() }));
  } else if (S.busy === 'folder' || !S.view) {
    content = h('div', { class: 'phone-browser__empty' }, busyLine('Caricamento cartelle…'));
  } else {
    const list = S.view.folders || [];
    const anySub = list.some((f) => f.sub);
    metaText = S.view.media
      ? `${plural(S.view.media, 'file diretto', 'file diretti')}${anySub ? ' · sottocartelle incluse' : ''}`
      : (anySub ? 'Sottocartelle incluse nell’importazione' : 'Cartella vuota');
    content = list.length
      ? h('ul', { class: 'phone-list' }, ...list.map(folderRow))
      : h('p', { class: 'muted md-typescale-body-medium phone-browser__empty' }, 'Nessuna sottocartella.');
  }
  box.replaceChildren(h('section', { class: 'card phone-browser' },
    h('div', { class: 'phone-browser__head' },
      crumbs(),
      metaText ? h('span', { class: 'phone-browser__meta' }, metaText) : null),
    content));
  if (focusKey) [...box.querySelectorAll('md-checkbox')].find((c) => c.dataset.key === focusKey)?.focus();
}

function historyRow(r) {
  const folders = (r.folders || []).join(', ');
  const parts = [
    `${formatNumber(r.new_files)} copiati`,
    r.skipped_known ? `${formatNumber(r.skipped_known)} saltati` : null,
    r.errors ? `${formatNumber(r.errors)} errori` : null,
  ].filter(Boolean);
  return h('li', { class: 'phone-history__row' },
    h('div', { class: 'phone-history__head' },
      h('span', { class: 'phone-history__when' },
        icon('smartphone'),
        h('strong', {}, r.device || 'Telefono'),
        h('span', { class: 'muted' }, '· ' + formatDateTime(r.when))),
      h('span', { class: `phone-history__state phone-history__state--${r.state}` }, STATE_LABEL[r.state] || r.state)),
    h('div', { class: 'phone-history__meta' },
      folders ? h('span', {}, folders) : null,
      h('strong', {}, parts.join(' · ') || 'Nessun file copiato')),
    r.dest ? h('code', { class: 'phone-history__dest' }, r.dest) : null,
    r.error ? h('div', { class: 'phone-hint phone-history__error' }, r.error) : null);
}

const STATE_LABEL = { done: 'Completata', cancelled: 'Annullata', error: 'Non riuscita' };

function renderHistory() {
  const box = UI.histSlot;
  if (!box) return;
  const list = S.history || [];
  box.replaceChildren(h('section', { class: 'card phone-history' },
    h('h2', { class: 'section-title md-typescale-title-medium phone-h2' }, 'Storico importazioni'),
    list.length
      ? h('ul', { class: 'phone-history__list' }, ...list.slice(0, 50).map(historyRow))
      : h('p', { class: 'phone-hint' }, 'Nessuna importazione ancora.')));
}

/** Pagina completa: usata all'apertura e al cambio di telefono/ricerca. Le sezioni poi si aggiornano da sole. */
function render() {
  UI.jobSlot = h('div', { class: 'phone-slot' });
  UI.devSlot = h('div', { class: 'phone-slot' });
  UI.selSlot = h('div', { class: 'phone-slot phone-slot--side' });
  UI.listSlot = h('div', { class: 'phone-slot phone-slot--main' });
  UI.histSlot = h('div', { class: 'phone-slot' });
  jobKey = null;
  const workspace = h('div', { class: 'phone-workspace' }, UI.listSlot, UI.selSlot);
  UI.root.replaceChildren(h('div', { class: 'view phone-page' },
    header(), UI.jobSlot, UI.devSlot, workspace, UI.histSlot));
  renderJob();
  renderDevices();
  renderSelection();
  renderList();
  renderHistory();
}

// ---------------------------------------------------------------- import
async function startImport() {
  const folders = [...S.selected.values()];
  const dest = S.dest.trim();
  if (!folders.length || !dest) return;
  const base = { device: S.device, storage: S.storage, folders, dest };
  const nFold = folders.length;
  const foldersTxt = `${nFold} ${nFold === 1 ? 'cartella' : 'cartelle'}`;

  // 1) anteprima: quanti file sono nuovi e quanti gia' presenti (nome e dimensione), senza copiare nulla
  let scan;
  S.scanning = true; updateActions();
  try {
    scan = await api.post('/phone/scan', base);
  } catch (e) {
    showSnackbar(errText(e), { error: true });
    return;
  } finally {
    S.scanning = false; updateActions();
  }

  if (!scan.total) { showSnackbar('Nelle cartelle scelte non ci sono foto o video.', { error: true }); return; }
  if (!scan.new) { showSnackbar('Nessun file nuovo da copiare: sono già tutti presenti.'); return; }

  // 2) scelta: se ci sono gia' presenti, l'utente decide se includerli
  let skip = [];
  if (scan.known > 0) {
    const choice = await choiceDialog({
      title: 'Foto già presenti',
      message: `Su ${formatNumber(scan.total)} file, ${formatNumber(scan.known)} risultano già presenti (stesso nome e stessa dimensione nell'archivio o nella destinazione). Il controllo non confronta il contenuto.`,
      confirmLabel: 'Importa',
      options: [
        { value: 'new', label: `Solo le nuove (${formatNumber(scan.new)})`, description: 'Le già presenti non vengono copiate dal telefono.' },
        { value: 'all', label: `Tutte (${formatNumber(scan.total)})`, description: `Copia anche le ${formatNumber(scan.known)} già presenti: saranno copie in più dei file che hai già.` },
      ],
    });
    if (!choice) return;
    if (choice === 'new') skip = scan.known_items.map((k) => k.rel);
  } else {
    const ok = await confirmDialog({
      title: 'Importare dal telefono?',
      message: `Copio ${formatNumber(scan.new)} file (${foldersTxt}) in:\n${dest}\nIl telefono non viene modificato.`,
      confirmLabel: 'Importa',
    });
    if (!ok) return;
  }

  // 3) copia come job sul server
  S.result = null;
  try {
    const job = await api.post('/phone/import', { ...base, skip });
    S.jobId = job.id; S.job = job; S.cancelling = false;
    renderJob(); updateActions();
    UI.jobSlot.scrollIntoView({ block: 'start', behavior: matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth' });
  } catch (e) {
    showSnackbar(errText(e), { error: true });
  }
}

export async function mount(root, ctx) {
  loadCss();
  UI = { root, ctx };
  const off = onEvent('job', (j) => {
    if (!j || j.name !== JOB || j.id !== S.jobId) return;
    S.job = j;
    if (j.state !== 'running') {
      S.cancelling = false;
      S.result = j.result || null;
      if (j.state === 'done') showSnackbar(`Copia completata: ${formatNumber(j.result?.new_files || 0)} file nuovi.`);
      loadHistory(UI.ctx).then(() => renderHistory());
    }
    renderJob(); updateActions();
  });
  await loadHistory(ctx);
  // Se un job e' ancora in corso (es. dopo cambio pagina) riprendi a mostrarlo.
  if (S.jobId && !S.job) {
    try { S.job = await api.get(`/jobs/${S.jobId}`, null, ctx.signal); } catch { /* ignora */ }
  }
  render();
  return () => { off(); };
}
