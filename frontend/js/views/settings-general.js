// Impostazioni > Generale: cartelle, aspetto, creazione, duplicati, calcolo, volti, informazioni.
// Contratto con il contenitore (settings.js): mountSection(container, {settings, draft, onDirty}) -> {destroy, setServerErrors, reveal}.
// Le modifiche restano in bozza: onDirty({values, patch, dirty, invalid}) e si salvano dalla barra del contenitore.
// Il tema non e' un'impostazione del server: si applica subito con applyTheme.
import { h, icon, debounce } from '../ui/el.js';
import { api } from '../api.js';
import { applyTheme, store } from '../store.js';
import { filterChips } from '../ui/chips.js';
import { confirmDialog } from '../ui/dialog.js';
import { skeletonList } from '../ui/skeleton.js';
import { showSnackbar } from '../ui/snackbar.js';
import { formatBytes, formatNumber } from '../ui/format.js';
import { mountHardware } from './settings-hardware.js';
import { mountExpert } from './settings-expert.js';

// Campi del server gestiti da questa sezione (solo impostazioni generiche dell'app).
const KEYS = ['source_dir', 'model_name', 'device', 'batch_size', 'workers', 'enable_faces'];
const FALLBACK_MODELS = [
  { id: 'google/siglip2-base-patch16-224', label: 'SigLIP 2 Base (224×224) — Veloce', hint: 'Bilanciato e leggero (download ~1,5 GB). Ottimo sia su CPU che su GPU.' },
  { id: 'google/siglip2-large-patch16-384', label: 'SigLIP 2 Large (384×384) — Alta precisione', hint: 'Quasi 3× i pixel (download ~3,5 GB): riconosce dettagli più fini. Consigliata GPU NVIDIA.' },
  { id: 'google/siglip2-so400m-patch14-384', label: 'SigLIP 2 SO400M (384×384) — Massima precisione', hint: 'Il modello più accurato (download ~4,5 GB), eccellente in italiano. Consigliata GPU NVIDIA.' },
];
const DEVICE_LABEL = { cuda: 'scheda video NVIDIA (CUDA)', cpu: 'processore', mps: 'chip Apple (MPS)' };
const STATUS_TTL_MS = 30000;
const STYLE_ID = 'fs-settings-general-style';
const CSS = `
.settings-group { display: flex; flex-direction: column; gap: var(--sp-3); min-width: 0; }
.settings-group__title {
  margin: 0; color: var(--md-sys-color-on-surface);
  font: 500 var(--md-sys-typescale-title-medium-size) / var(--md-sys-typescale-title-medium-line-height) var(--md-ref-typeface-plain);
}
.settings-card {
  display: flex; flex-direction: column; gap: var(--sp-4); min-width: 0;
  padding: var(--sp-5) var(--sp-6); border-radius: var(--shape-lg);
  border: 1px solid var(--md-sys-color-outline-variant);
  background: var(--md-sys-color-surface-container); color: var(--md-sys-color-on-surface);
}
.settings-field { display: block; width: 100%; }
.settings-folder { display: flex; flex-wrap: wrap; align-items: flex-start; gap: var(--sp-2); }
.settings-folder .settings-field { flex: 1 1 180px; min-width: 0; }
.settings-folder md-outlined-button { flex: none; margin-top: var(--sp-1); }
.settings-path { overflow-wrap: anywhere; }
.settings-info details summary { cursor: pointer; }
.callout[hidden] { display: none; }
@media (max-width: 599px) {
  .settings-folder { flex-direction: column; align-items: stretch; }
  .settings-folder .settings-field { flex: none; }
  .settings-folder md-outlined-button { margin-top: 0; align-self: flex-start; }
}
@media (pointer: coarse) {
  .settings-chips md-filter-chip, .settings-switch md-switch { min-height: 48px; }
}
.settings-radios { display: flex; flex-direction: column; gap: var(--sp-1); }
.settings-radio {
  display: flex; align-items: flex-start; gap: var(--sp-2);
  padding: var(--sp-1) 0; cursor: pointer;
}
.settings-radio md-radio { flex: none; margin-top: 2px; }
.settings-radio__text { display: flex; flex-direction: column; min-width: 0; }
.settings-radio__hint { color: var(--md-sys-color-on-surface-variant); }
.settings-switch { display: flex; align-items: center; justify-content: space-between; gap: var(--sp-4); }
.settings-switch__text { display: flex; flex-direction: column; min-width: 0; }
.settings-hint { margin: 0; color: var(--md-sys-color-on-surface-variant); }
.settings-numbers { display: flex; flex-wrap: wrap; gap: var(--sp-4); }
.settings-numbers .settings-field { flex: 1 1 200px; }
.settings-info {
  display: grid; grid-template-columns: max-content minmax(0, 1fr);
  gap: var(--sp-2) var(--sp-4); margin: 0;
}
.settings-info dt { color: var(--md-sys-color-on-surface-variant); }
.settings-info dd { margin: 0; min-width: 0; overflow-wrap: anywhere; }
.settings-stack { display: flex; flex-direction: column; gap: var(--sp-3); min-width: 0; }
.hw-table { display: flex; flex-direction: column; min-width: 0; }
.hw-row {
  display: grid; grid-template-columns: minmax(0, 2.2fr) minmax(0, 1fr) minmax(0, 1fr);
  gap: var(--sp-1) var(--sp-4); align-items: center; padding: var(--sp-2) 0;
  border-top: 1px solid var(--md-sys-color-outline-variant);
}
.hw-row--head { border-top: 0; color: var(--md-sys-color-on-surface-variant); }
.hw-row--rec { font-weight: 500; }
.hw-name { display: flex; flex-wrap: wrap; align-items: center; gap: var(--sp-2); min-width: 0; }
.hw-badge {
  padding: 2px var(--sp-2); border-radius: var(--shape-full); white-space: nowrap;
  background: var(--md-sys-color-primary-container); color: var(--md-sys-color-on-primary-container);
}
.hw-badge--warn { background: var(--md-sys-color-tertiary-container); color: var(--md-sys-color-on-tertiary-container); }
.hw-sub { color: var(--md-sys-color-on-surface-variant); }
.hw-actions { display: flex; flex-wrap: wrap; align-items: center; gap: var(--sp-3); }
.hw-progress { display: flex; flex-direction: column; gap: var(--sp-2); }
.hw-progress md-linear-progress { width: 100%; }
.xp-details > summary { cursor: pointer; padding: var(--sp-1) 0; }
.xp-details[open] > summary { margin-bottom: var(--sp-3); }
.xp-list { display: flex; flex-direction: column; }
.xp-model { padding: var(--sp-3) 0; border-top: 1px solid var(--md-sys-color-outline-variant); }
.xp-model:first-child { border-top: 0; }
.xp-values {
  display: grid; grid-template-columns: max-content minmax(0, 1fr);
  gap: var(--sp-1) var(--sp-4); margin: 0;
}
.xp-values dt { color: var(--md-sys-color-on-surface-variant); }
.xp-values dd { margin: 0; font-variant-numeric: tabular-nums; }
@media (max-width: 599px) {
  .hw-row { grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); }
  .hw-row > :first-child { grid-column: 1 / -1; }
  .hw-row--head { display: none; }
}
`;

let uid = 0;
let statusCache = null; // {at, value}: GET /status condiviso per qualche secondo (il montaggio puo' ripetersi)

function injectStyle() {
  if (document.getElementById(STYLE_ID)) return;
  const style = document.createElement('style');
  style.id = STYLE_ID;
  style.textContent = CSS;
  document.head.append(style);
}

/** Normalizza un percorso Windows per il confronto (minuscole, barre uniformi, senza barra finale). */
const norm = (p) => p.trim().replace(/[\\/]+$/, '').replace(/\//g, '\\').toLowerCase();

/** True se `child` coincide con `parent` o ne e' una sottocartella. */
function inside(child, parent) {
  const c = norm(child);
  const p = norm(parent);
  return !!c && !!p && (c === p || c.startsWith(p + '\\'));
}

const sameValue = (a, b) => JSON.stringify(a) === JSON.stringify(b);

/** Intero in [min, max] dal testo del campo, altrimenti null. */
function toInt(raw, min, max) {
  const s = String(raw).trim();
  const n = Number(s);
  return s !== '' && Number.isInteger(n) && n >= min && n <= max ? n : null;
}

/** Gruppo di md-radio con spiegazione per ogni scelta (la riga intera e' cliccabile). */
function radioGroup({ name, labelId, options, value, onPick }) {
  const group = h('div', { class: 'settings-radios', role: 'radiogroup', 'aria-labelledby': labelId });
  for (const opt of options) {
    const radio = document.createElement('md-radio');
    radio.name = name;
    radio.setAttribute('value', opt.value);
    radio.checked = value === opt.value;
    radio.setAttribute('aria-label', opt.label);
    radio.addEventListener('change', () => { if (radio.checked) onPick(opt.value); });
    const row = h('div', { class: 'settings-radio' },
      radio,
      h('div', { class: 'settings-radio__text' },
        h('span', { class: 'md-typescale-body-large' }, opt.label),
        h('span', { class: 'settings-radio__hint md-typescale-body-medium' }, opt.hint)));
    row.addEventListener('click', (e) => { if (e.target !== radio) radio.click(); });
    group.append(row);
  }
  return group;
}

/** Valori grezzi (testo per i numeri) da GET /settings. */
function rawValues(src) {
  return {
    source_dir: src.source_dir || '',
    model_name: src.model_name || FALLBACK_MODELS[0].id,
    device: src.device,
    batch_size: String(src.batch_size),
    workers: String(src.workers),
    enable_faces: !!src.enable_faces,
  };
}

export function mountSection(container, { settings = {}, draft = null, onDirty = () => {} } = {}) {
  injectStyle();
  const uidPrefix = `sg${++uid}`;
  const saved = rawValues(settings);
  const values = { ...saved, ...(draft || {}) };
  const folderErr = {};   // cartella -> errore della verifica sul server ('' = ok)
  const serverErr = {};   // campo -> errore restituito dall'ultimo salvataggio
  const disks = {};       // cartella -> risposta di GET /system/disk
  const seqs = {};        // invalida le verifiche cartella superate
  const folderFields = {};
  const numberFields = {};
  const enableSwitch = document.createElement('md-switch');
  let disposed = false;
  let st = { patch: {}, errors: {}, dirty: false, invalid: false };

  /** Calcola patch, errori di validazione locale e stato sporco. */
  function compute() {
    const errors = {};
    const s = values.source_dir.trim();
    const batch = toInt(values.batch_size, 1, 512);
    const workers = toInt(values.workers, 1, 32);
    if (batch === null) errors.batch_size = 'Inserisci un numero intero tra 1 e 512.';
    if (workers === null) errors.workers = 'Inserisci un numero intero tra 1 e 32.';
    const parsed = {
      source_dir: s, model_name: values.model_name,
      device: values.device, batch_size: batch, workers, enable_faces: !!values.enable_faces,
    };
    const patch = {};
    const bad = { ...errors };
    for (const k of KEYS) {
      if (folderErr[k]) bad[k] = folderErr[k];
      if (bad[k]) continue;
      if (!sameValue(parsed[k], coerceSaved(k, saved[k]))) patch[k] = parsed[k];
    }
    const invalid = Object.keys(bad).length > 0;
    return { patch, errors: bad, dirty: Object.keys(patch).length > 0 || invalid, invalid };
  }

  /** Il valore salvato nello stesso tipo del valore analizzato (per il confronto). */
  function coerceSaved(k, v) {
    if (k === 'source_dir' || k === 'output_dir') return String(v).trim();
    if (k === 'batch_size' || k === 'workers') return Number(v);
    return v;
  }

  /** Messaggio mostrato per un campo: prima quello locale o di verifica, poi quello del server. */
  const shown = (k) => st.errors[k] || serverErr[k] || '';

  /** Riallinea errori, testi di supporto e comunica lo stato al contenitore. */
  function refresh() {
    if (disposed) return;
    st = compute();
    paint();
    onDirty({ values: { ...values }, patch: st.patch, dirty: st.dirty, invalid: st.invalid });
  }

  function paint() {
    for (const key of Object.keys(folderFields)) {
      const { tf } = folderFields[key];
      const msg = shown(key);
      tf.error = !!msg;
      tf.errorText = msg;
      tf.supportingText = msg ? '' : supportText(key);
    }
    for (const key of Object.keys(numberFields)) {
      const { tf, help } = numberFields[key];
      const msg = shown(key);
      tf.error = !!msg;
      tf.errorText = msg;
      tf.supportingText = msg ? '' : help;
    }
    if (enableSwitch.selected !== !!values.enable_faces) enableSwitch.selected = !!values.enable_faces;
  }

  // ---- cartelle
  function supportText(key) {
    if (key === 'source_dir') return 'Le foto originali non vengono mai modificate né cancellate.';
    const d = disks[key];
    if (!d || d.free_bytes == null) return 'Qui vengono create le cartelle ordinate.';
    const free = `Spazio libero sul disco: ${formatBytes(d.free_bytes)}`;
    return d.exists ? free : `Non esiste ancora: verrà creata al primo utilizzo. ${free}`;
  }

  /** Verifica sul server che la cartella esista e legge lo spazio libero (non blocca la digitazione). */
  async function checkFolder(key) {
    if (disposed) return;
    const my = (seqs[key] = (seqs[key] || 0) + 1);
    const val = values[key].trim();
    disks[key] = null;
    folderErr[key] = '';
    if (val) {
      try {
        const d = await api.get('/system/disk', { path: val });
        if (disposed || my !== seqs[key]) return;
        if (d.is_file) folderErr[key] = 'Questo è un file, non una cartella.';
        else if (!d.exists && key === 'source_dir') folderErr[key] = 'Questa cartella non esiste.';
        disks[key] = d;
      } catch (e) {
        if (disposed || my !== seqs[key]) return;
        folderErr[key] = e.status === 0 ? e.message : 'Impossibile verificare questa cartella.';
      }
    }
    refresh();
  }

  const checkLater = {
    source_dir: debounce(() => checkFolder('source_dir'), 400),
    output_dir: debounce(() => checkFolder('output_dir'), 400),
  };

  function folderRow(key, label) {
    const tf = document.createElement('md-outlined-text-field');
    tf.className = 'settings-field';
    tf.label = label;
    tf.value = values[key];
    folderFields[key] = { tf };
    tf.addEventListener('input', () => {
      values[key] = tf.value;
      delete serverErr[key];
      checkLater[key]();
      refresh();
    });
    tf.addEventListener('change', () => { values[key] = tf.value; checkFolder(key); });

    const btnText = document.createTextNode('Sfoglia…');
    const btn = h('md-outlined-button', {}, icon('folder_open', { slot: 'icon' }), btnText);
    btn.setAttribute('aria-label', `Sfoglia per scegliere: ${label.toLowerCase()}`);
    btn.addEventListener('click', async () => {
      btn.disabled = true;
      btn.setAttribute('aria-busy', 'true');
      btnText.textContent = 'Apro la finestra…';
      try {
        const res = await api.post('/system/pick-folder', { title: label, initial: values[key] || undefined });
        if (disposed) return;
        if (res && res.available === false) {
          showSnackbar('La finestra di scelta cartella non è disponibile: scrivi il percorso a mano.', { error: true });
          return;
        }
        if (!res || !res.path) return;
        values[key] = res.path;
        tf.value = res.path;
        delete serverErr[key];
        checkFolder(key);
      } catch (e) {
        if (!disposed) showSnackbar(e.message, { error: true });
      } finally {
        btn.disabled = false;
        btn.removeAttribute('aria-busy');
        btnText.textContent = 'Sfoglia…';
      }
    });
    return h('div', { class: 'settings-folder' }, tf, btn);
  }

  // ---- numeri interi (lotto, thread)
  function numberField(key, label, min, max, help) {
    const tf = document.createElement('md-outlined-text-field');
    tf.className = 'settings-field';
    tf.label = label;
    tf.type = 'number';
    tf.value = values[key];
    tf.supportingText = help;
    tf.setAttribute('min', String(min));
    tf.setAttribute('max', String(max));
    numberFields[key] = { tf, help };
    tf.addEventListener('input', () => {
      values[key] = tf.value;
      delete serverErr[key];
      refresh();
    });
    return tf;
  }

  // ---- blocchi che dipendono da GET /status (modello, ffmpeg, dispositivo, contatori)
  const labelId = (name) => `${uidPrefix}-${name}`;
  const modelSlot = h('div', { class: 'settings-stack' });
  const calcSlot = h('div', { class: 'settings-stack' }, skeletonList(2));
  const facesSlot = h('div', { class: 'settings-stack' });
  const infoSlot = h('div', { class: 'settings-stack' }, skeletonList(2));
  const modelTitle = labelId('model-title');

  let lastModels = FALLBACK_MODELS; // ultimo elenco mostrato: serve a ridisegnare la scelta quando si applica il consiglio
  function renderModelPicker(modelsList = lastModels) {
    lastModels = modelsList;
    const opts = modelsList.map((m) => ({
      value: m.id,
      label: m.cached === true ? `${m.label} · Scaricato` : m.label,
      hint: m.hint,
    }));
    if (values.model_name && !opts.some((o) => o.value === values.model_name)) {
      opts.push({ value: values.model_name, label: values.model_name, hint: 'Modello personalizzato configurato.' });
    }
    modelSlot.replaceChildren(
      h('p', { id: modelTitle, class: 'settings-hint md-typescale-body-medium' },
        'Scegli il modello AI per ricerca semantica, etichette e album suggeriti. Cambiare modello richiede una nuova analisi degli embedding (foto, album e preferiti restano intatti).'),
      radioGroup({
        name: `${uidPrefix}-model`,
        labelId: modelTitle,
        value: values.model_name,
        onPick: (v) => { values.model_name = v; refresh(); },
        options: opts,
      }));
  }
  renderModelPicker();

  async function resetEmbeddingsForNewModel() {
    const ok = await confirmDialog({
      title: 'Preparare la re-indicizzazione con il nuovo modello?',
      message: 'Verranno azzerati solo gli embedding del vecchio modello. Tutte le foto, i metadati, i preferiti e i tuoi album personali restano intatti. Poi premi «Avvia» nella pagina Analisi.',
      confirmLabel: 'Prepara re-indicizzazione',
      danger: true,
    });
    if (!ok || disposed) return;
    try {
      const r = await api.post('/index/reset-embeddings', { confirm: true });
      if (disposed) return;
      statusCache = null;
      showSnackbar(`${formatNumber(r.reset || 0)} file pronti per essere analizzati con il nuovo modello.`);
      location.hash = '#/organize?tab=index';
    } catch (e) {
      if (!disposed) showSnackbar(e.message || 'Operazione non riuscita.', { error: true });
    }
  }

  function renderStatus(status) {
    if (Array.isArray(status.available_models) && status.available_models.length) {
      renderModelPicker(status.available_models);
    }
    const cached = status.model_cached
      ? 'Il modello selezionato è già scaricato su questo computer.'
      : 'Il modello selezionato verrà scaricato al primo utilizzo (serve una connessione internet).';
    const inUse = DEVICE_LABEL[status.device] || status.device || '—';
    const mismatchCallout = status.model_mismatch
      ? h('div', { class: 'callout callout--warning', role: 'status' },
        h('span', { class: 'callout__icon material-symbols-outlined', 'aria-hidden': 'true' }, 'sync_problem'),
        h('div', { class: 'settings-stack' },
          h('p', { class: 'callout__title' }, 'Modello cambiato: serve una nuova analisi'),
          h('p', {}, `Le foto attuali sono state analizzate con «${status.embedding_model}», mentre ora è selezionato «${status.model}». `
            + 'Azzera gli embedding del vecchio modello e avvia l’analisi per usare il nuovo modello.'),
          h('div', {},
            h('md-filled-tonal-button', { onclick: () => resetEmbeddingsForNewModel() },
              icon('restart_alt', { slot: 'icon' }), 'Prepara re-indicizzazione'))))
      : null;
    calcSlot.replaceChildren(
      mismatchCallout,
      h('p', { class: 'settings-hint md-typescale-body-medium' }, `In uso ora: ${inUse}.`),
      h('p', { class: 'md-typescale-body-medium' }, cached),
      h('p', { class: 'settings-hint md-typescale-body-small' }, `Modello tecnico attivo: ${status.model || '—'}`),
      status.ffmpeg
        ? h('p', { class: 'settings-hint md-typescale-body-medium' }, 'ffmpeg: installato, le anteprime dei video funzionano.')
        : h('div', { class: 'callout callout--warning', role: 'note' },
          h('span', { class: 'callout__icon material-symbols-outlined', 'aria-hidden': 'true' }, 'warning'),
          h('div', {},
            h('p', { class: 'callout__title' }, 'ffmpeg non trovato'),
            h('p', {}, 'Senza ffmpeg non si possono estrarre le anteprime dei video. Installalo e riavvia PhotoScan.'))),
    );
    // Stato riconoscimento volti
    const fi = status.faces || {};
    if (fi.available === false) {
      facesSlot.replaceChildren(
        h('div', { class: 'callout callout--warning', role: 'note' },
          h('span', { class: 'callout__icon material-symbols-outlined', 'aria-hidden': 'true' }, 'warning'),
          h('div', {},
            h('p', { class: 'callout__title' }, 'Motore volti non disponibile'),
            h('p', {}, fi.reason || 'Installa onnxruntime per abilitare il riconoscimento volti.'))));
    } else {
      const provText = fi.provider === 'cuda'
        ? 'Accelerazione: scheda video NVIDIA (CUDA).'
        : 'Accelerazione: processore (CPU).';
      const modText = fi.models_ready
        ? 'Modelli volti locali: pronti su questo computer.'
        : 'Modelli volti locali: verranno scaricati al primo avvio dalla sezione Persone (~280 MB).';
      facesSlot.replaceChildren(
        h('p', { class: 'settings-hint md-typescale-body-medium' }, `${provText} ${modText}`));
    }
    const c = status.counts || {};
    infoSlot.replaceChildren(
      h('dl', { class: 'settings-info md-typescale-body-medium' },
        h('dt', {}, 'Versione'), h('dd', {}, status.version || '—'),
        h('dt', {}, 'Archivio foto'),
        h('dd', {}, h('details', {}, h('summary', {}, 'Mostra il percorso'), h('span', { class: 'settings-path' }, status.db_path || '—'))),
        h('dt', {}, 'Dimensione archivio'),
        h('dd', {}, status.catalog_bytes != null ? formatBytes(status.catalog_bytes) : '—'),
        h('dt', {}, 'Foto'), h('dd', {}, formatNumber(c.foto)),
        h('dt', {}, 'Video'), h('dd', {}, formatNumber(c.video)),
        h('dt', {}, 'Analizzate'), h('dd', {}, formatNumber(c.indicizzati))),
    );
  }

  const statusError = () => h('p', { class: 'settings-hint md-typescale-body-medium' }, 'Impossibile leggere lo stato del programma.');
  const fresh = false; // ricarica sempre /status all'apertura per riflettere subito un cambio modello salvato
  (fresh ? Promise.resolve(statusCache.value) : api.get('/status').then((s) => {
    statusCache = { at: Date.now(), value: s };
    return s;
  }))
    .then((s) => { if (!disposed) renderStatus(s); })
    .catch(() => {
      if (disposed) return;
      calcSlot.replaceChildren(statusError());
      infoSlot.replaceChildren(statusError());
    });

  // ---- composizione della pagina
  const group = (title, children) => {
    const id = `${uidPrefix}-${title.replace(/\W+/g, '-').toLowerCase()}`;
    return h('section', { class: 'settings-group', 'aria-labelledby': id },
      h('h2', { id, class: 'settings-group__title' }, title),
      h('div', { class: 'settings-card' }, children));
  };

  // Cartella delle foto (solo sorgente)
  const folders = group('Cartella delle foto', [
    h('p', { class: 'settings-hint md-typescale-body-medium' },
      'La cartella che contiene il tuo archivio fotografico. PhotoScan la legge senza mai modificarla automaticamente.'),
    h('div', { class: 'settings-stack' },
      folderRow('source_dir', 'Cartella delle foto')),
  ]);

  // Aspetto: il tema si applica subito (non e' salvato sul server)
  const themeChips = filterChips(
    [
      { id: 'system', label: 'Sistema', icon: 'brightness_auto' },
      { id: 'light', label: 'Chiaro', icon: 'light_mode' },
      { id: 'dark', label: 'Scuro', icon: 'dark_mode' },
    ],
    {
      selected: store.get('theme', 'system'),
      label: 'Tema',
      onChange: (id) => applyTheme(id),
    },
  );
  const appearance = group('Aspetto', [
    h('p', { class: 'settings-hint md-typescale-body-medium' }, 'Scegli se usare il tema chiaro, scuro o quello del sistema.'),
    themeChips,
  ]);
  // Il tema puo' cambiare anche dal pulsante della barra in alto: le scelte devono seguirlo.
  themeChips.classList.add('settings-chips');
  const unsubTheme = store.subscribe('theme', (mode) => themeChips.setSelected(mode));

  // Il tuo computer: configurazione rilevata, consiglio e velocita' prevista (settings-hardware.js)
  const hwSlot = h('div', { class: 'settings-stack' });
  function applyRecommendation(rec) {
    values.model_name = rec.model;
    values.batch_size = String(rec.batch_size);
    values.workers = String(rec.workers);
    for (const k of ['batch_size', 'workers']) if (numberFields[k]) numberFields[k].tf.value = values[k];
    renderModelPicker();
    delete serverErr.model_name; delete serverErr.batch_size; delete serverErr.workers;
    refresh();
  }
  const hwMount = mountHardware(hwSlot, { current: () => values, onApply: applyRecommendation });
  const hardware = group('Il tuo computer', [
    h('p', { class: 'settings-hint md-typescale-body-medium' },
      'PhotoScan riconosce la scheda video, il processore e la memoria, e propone il modello più adatto. '
      + 'Puoi anche misurare quante foto al secondo riesce ad analizzare.'),
    hwSlot,
  ]);

  // Calcolo
  const deviceTitle = labelId('device-title');
  const deviceOptions = [
    { value: 'auto', label: 'Automatico (consigliato)', hint: 'Usa la scheda video NVIDIA se c’è, altrimenti il processore.' },
    { value: 'cuda', label: 'Scheda video NVIDIA', hint: 'Più veloce. Richiede una scheda NVIDIA con i driver CUDA.' },
    { value: 'cpu', label: 'Processore', hint: 'Funziona su qualsiasi computer, ma è più lento.' },
  ];
  if (values.device === 'mps') deviceOptions.push({ value: 'mps', label: 'Chip Apple (MPS)', hint: 'Acceleratore dei Mac con chip Apple.' });
  const deviceGroup = radioGroup({
    name: `${uidPrefix}-device`,
    labelId: deviceTitle,
    value: values.device,
    onPick: (v) => { values.device = v; refresh(); },
    options: deviceOptions,
  });
  const calc = group('Modello AI e calcolo', [
    modelSlot,
    calcSlot,
    h('hr', {}),
    h('p', { id: deviceTitle, class: 'settings-hint md-typescale-body-medium' }, 'Dove eseguire il riconoscimento delle foto.'),
    deviceGroup,
    h('div', { class: 'settings-numbers' },
      numberField('batch_size', 'Foto per volta', 1, 512, 'Valori più alti sono più veloci ma usano più memoria.'),
      numberField('workers', 'Elaborazioni contemporanee', 1, 32, 'Quanti file elaborare insieme.')),
  ]);

  // Riconoscimento volti
  const facesTitle = labelId('faces-title');
  enableSwitch.selected = !!values.enable_faces;
  enableSwitch.setAttribute('aria-labelledby', facesTitle);
  enableSwitch.addEventListener('change', () => { values.enable_faces = enableSwitch.selected; refresh(); });
  const faces = group('Riconoscimento volti', [
    h('div', { class: 'settings-switch' },
      h('div', { class: 'settings-switch__text' },
        h('span', { id: facesTitle, class: 'md-typescale-body-large' }, 'Attiva riconoscimento volti'),
        h('span', { class: 'settings-hint md-typescale-body-medium' },
          'Raggruppa automaticamente le persone nelle tue foto. Tutta l’elaborazione avviene in locale sul tuo PC: nessuna immagine viene inviata online.')),
      enableSwitch),
    facesSlot,
  ]);

  // Per esperti: calibrazione dei modelli e modelli personalizzati (settings-expert.js), chiusa di default
  const expertSlot = h('div', { class: 'settings-stack' });
  let expertMount = null;
  const expertDetails = h('details', { class: 'xp-details' },
    h('summary', { class: 'md-typescale-body-large' }, 'Mostra calibrazione dei modelli e modelli personalizzati'),
    expertSlot);
  expertDetails.addEventListener('toggle', () => {
    if (expertDetails.open && !expertMount) {
      expertMount = mountExpert(expertSlot, {
        onModelsChanged: () => api.get('/status').then((s) => {
          statusCache = { at: Date.now(), value: s };
          if (!disposed) renderStatus(s);
        }).catch(() => {}),
      });
    }
  });
  const expert = group('Per esperti', [
    h('p', { class: 'settings-hint md-typescale-body-medium' },
      'Soglie e temperatura di ogni modello AI, preset dei modelli testati, calibrazione automatica sul tuo archivio '
      + 'e possibilità di usare un tuo modello di embedding.'),
    expertDetails,
  ]);

  // Informazioni
  const info = group('Informazioni', [infoSlot]);

  container.replaceChildren(folders, appearance, hardware, calc, faces, expert, info);
  if (values.source_dir.trim()) checkFolder('source_dir');
  refresh();

  return {
    destroy() { disposed = true; unsubTheme(); hwMount.destroy(); expertMount?.destroy(); },
    /** Errori di validazione del server per i campi di questa sezione. */
    setServerErrors(fields) {
      for (const k of KEYS) delete serverErr[k];
      for (const [k, msg] of Object.entries(fields || {})) if (KEYS.includes(k)) serverErr[k] = msg;
      paint();
    },
    reveal() { paint(); },
  };
}
