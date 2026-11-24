// Impostazioni: UNA schermata con le sezioni Generale, Rete e Aggiornamenti (Etichette e Suggerite si aprono dagli Album).
//
// Contratto con le sezioni (settings-*.js), tutte monte da qui:
//   mountSection(container, {settings, defaults, draft, onDirty}) -> {destroy(), setServerErrors(fields), reveal()}
//   - settings: ultimi valori salvati (GET /settings); defaults: valori di fabbrica (GET /settings/defaults) o null.
//   - draft: ultimi `values` della sezione (sopravvivono al cambio scheda), o null.
//   - onDirty({values, patch, dirty, invalid}): a ogni modifica. patch = solo i campi cambiati e validi;
//     dirty = ci sono modifiche (anche non valide); invalid = ci sono campi da correggere.
//   - setServerErrors({campo: messaggio}): errori di validazione del server accanto ai campi.
//   - reveal(): mostra gli errori di tutti i campi (dopo un tentativo di salvataggio).
// Il contenitore possiede le bozze: unica barra "Annulla / Salva" (PUT /settings con la patch unita) e unico
// riquadro "Ricalcola il piano" (POST /plan/rebuild + evento SSE "job") dopo modifiche a regole o etichette.
import { h, icon } from '../ui/el.js';
import { api, onEvent } from '../api.js';
import { store } from '../store.js';
import { showSnackbar } from '../ui/snackbar.js';
import { confirmDialog } from '../ui/dialog.js';
import { emptyState, errorState } from '../ui/empty-state.js';
import { skeletonCard } from '../ui/skeleton.js';

const TABS = [
  { id: 'general', label: 'Generale', file: './settings-general.js' },
  { id: 'network', label: 'Rete', file: './settings-network.js' },
  { id: 'updates', label: 'Aggiornamenti', file: './settings-updates.js' },
  { id: 'labels', label: 'Etichette', file: './settings-labels.js', wide: true },
  { id: 'suggested', label: 'Suggerite', file: './settings-suggested.js', wide: true },
];
// Campo -> sezione che lo possiede (per portare l'utente sul campo con l'errore del server).
const GENERAL_KEYS = ['source_dir', 'model_name', 'device', 'batch_size', 'workers', 'enable_faces'];
const LABEL_KEYS = ['labels', 'null_prompts', 'score_threshold'];
const OWNER = Object.fromEntries([
  ...GENERAL_KEYS.map((k) => [k, 'general']),
  ...LABEL_KEYS.map((k) => [k, 'labels']),
]);
// Modifiche che rendono il piano delle cartelle da ricalcolare.
const PLAN_KEYS = new Set([...LABEL_KEYS]);

const STYLE_ID = 'fs-settings-style';
const CSS = `
.settings-view > .view-header { max-width: 1080px; width: 100%; margin-inline: auto; }
.settings-body { min-width: 0; }
.settings-section { display: flex; flex-direction: column; gap: var(--sp-6); max-width: 1080px; width: 100%; margin-inline: auto; min-width: 0; }
.settings-section--wide { max-width: 1180px; }
.settings-savebar {
  position: sticky; bottom: var(--sp-3); z-index: var(--z-raised);
  display: flex; flex-wrap: wrap; align-items: center; gap: var(--sp-2) var(--sp-3);
  max-width: 1080px; width: 100%; margin: var(--sp-6) auto 0; padding: var(--sp-3) var(--sp-5);
  border-radius: var(--shape-md);
  border: 1px solid var(--md-sys-color-outline-variant);
  /* Elevazione tonale (tinta primary al 8% sopra surface-container-high) + ombra leggera, non ombra piena. */
  background: color-mix(in srgb, var(--md-sys-color-surface-tint) 8%, var(--md-sys-color-surface-container-high));
  color: var(--md-sys-color-on-surface);
  box-shadow: var(--elev-2);
}
.settings-savebar[hidden], .settings-plan[hidden] { display: none; }
.settings-savebar__text { flex: 1 1 200px; min-width: 0; margin: 0; }
.settings-savebar__actions { display: flex; flex-wrap: wrap; gap: var(--sp-2); margin-inline-start: auto; }
.settings-tabs { margin-top: var(--sp-2); margin-bottom: var(--sp-4); }
.settings-view .settings-tabs { max-width: 1080px; width: 100%; margin-inline: auto; }  /* stessa colonna centrata dei contenuti */
.settings-plan {
  display: flex; flex-direction: column; gap: var(--sp-3);
  max-width: 1080px; width: 100%; margin: var(--sp-6) auto 0; padding: var(--sp-5);
  border-radius: var(--shape-md);
  border: 1px solid var(--md-sys-color-outline-variant);
  background: var(--md-sys-color-surface-container);
  color: var(--md-sys-color-on-surface);
}
.settings-plan__title { margin: 0; font: 400 var(--md-sys-typescale-title-medium-size) / var(--md-sys-typescale-title-medium-line-height) var(--md-ref-typeface-plain); }
.settings-plan__text { margin: 0; color: var(--md-sys-color-on-surface-variant); }
.settings-plan__actions { display: flex; flex-wrap: wrap; align-items: center; gap: var(--sp-2); }
.settings-plan__actions a { text-decoration: none; }
@keyframes settings-rise { from { opacity: 0; transform: translateY(8px); } to { opacity: 1; transform: none; } }
@keyframes settings-fade { from { opacity: 0; } to { opacity: 1; } }
.settings-savebar:not([hidden]), .settings-plan-host:not([hidden]) { animation: settings-rise var(--dur-medium) var(--motion-standard); }
.settings-section { animation: settings-fade var(--dur-short) var(--motion-standard); }
@media (prefers-reduced-motion: reduce) { .settings-savebar, .settings-plan-host, .settings-section { animation: none; } }
`;

function injectStyle() {
  if (document.getElementById(STYLE_ID)) return;
  const style = document.createElement('style');
  style.id = STYLE_ID;
  style.textContent = CSS;
  document.head.append(style);
}

/** Messaggio d'errore per il salvataggio: quello del server se e' di validazione, altrimenti generico. */
function saveErrorText(e) {
  if (e.status === 0 || (e.status >= 400 && e.status < 500)) return e.message;
  return 'Impossibile salvare le impostazioni. Riprova.';
}

const pct = (ratio) => `${Math.round(ratio * 100)}%`;

export async function mount(root, { query = {}, navigate } = {}) {
  injectStyle();
  let settings = null;        // ultimi valori salvati sul server
  let defaults = null;        // valori di fabbrica (per "Ripristina predefiniti")
  let drafts = {};            // id sezione -> ultimo stato inviato con onDirty
  let pendingErrors = {};     // errori di validazione dell'ultimo salvataggio fallito (campo -> messaggio)
  const isLabelsMode = query.tab === 'labels' || query.tab === 'suggested';
  const activeTabs = isLabelsMode ? TABS.filter((t) => t.id !== 'general') : TABS.filter((t) => ['general', 'network', 'updates'].includes(t.id));
  let tabIndex = Math.max(0, activeTabs.findIndex((t) => t.id === query.tab));
  let selfHash = activeTabs[tabIndex].id !== 'general' ? `#/settings?tab=${activeTabs[tabIndex].id}` : '#/settings';
  let section = null;         // { id, api } della sezione montata
  let mountSeq = 0;           // invalida i montaggi e le callback delle sezioni superate
  let revealNext = false;     // mostra tutti gli errori della sezione appena montata (salvataggio non valido)
  let saving = false;
  let disposed = false;
  // ---- piano delle cartelle (un solo riquadro, condiviso da Regole ed Etichette)
  let planStale = false;      // regole/etichette salvate dopo l'ultimo ricalcolo
  let planPhase = 'idle';     // idle | running | error
  let planMsg = '';
  let progress = { done: 0, total: 0, message: '' };
  let jobId = null;
  let planSig = '';

  const dirtyAny = () => Object.values(drafts).some((d) => d.dirty);
  const invalidIds = () => activeTabs.map((t) => t.id).filter((id) => drafts[id]?.invalid);
  const mergedPatch = () => Object.assign({}, ...Object.values(drafts).map((d) => d.patch || {}));

  // ---- intestazione, schede e corpo
  const header = h('div', { class: 'view-header' },
    h('div', {},
      h('h1', { class: 'md-typescale-headline-medium' }, isLabelsMode ? 'Etichette e album suggeriti' : 'Impostazioni')),
    isLabelsMode
      ? h('div', { class: 'view-header-actions' },
        h('md-text-button', { onclick: () => navigate?.('/albums') }, icon('arrow_back', { slot: 'icon' }), 'Torna agli Album'))
      : null);
  const tabsEl = h('md-tabs', { class: 'settings-tabs', 'aria-label': 'Sezioni', hidden: activeTabs.length < 2 },
    ...activeTabs.map((t) => h('md-primary-tab', {}, t.label)));
  tabsEl.activeTabIndex = tabIndex;
  const body = h('div', { class: 'settings-body' });

  // ---- piano: riquadro "Ricalcola il piano" (sotto il contenuto, sopra la barra di salvataggio)
  const planHost = h('div', { class: 'settings-plan-host', hidden: true });

  // ---- unica barra di salvataggio
  const barText = h('p', { class: 'settings-savebar__text md-typescale-body-medium', role: 'status' }, 'Hai modifiche non salvate.');
  const cancelBtn = h('md-text-button', { onclick: () => discard() }, 'Annulla');
  const saveBtn = h('md-filled-button', { onclick: () => save() }, icon('save', { slot: 'icon' }), 'Salva');
  const bar = h('div', { class: 'settings-savebar', role: 'region', 'aria-label': 'Modifiche da salvare', hidden: true },
    barText,
    h('div', { class: 'settings-savebar__actions' }, cancelBtn, saveBtn));

  function updateBar() {
    if (disposed) return;
    const inv = invalidIds();
    bar.hidden = !dirtyAny();
    barText.textContent = inv.length
      ? 'Correggi i campi evidenziati per salvare.'
      : 'Hai modifiche non salvate.';
    saveBtn.disabled = saving;
    cancelBtn.disabled = saving;
    if (dirtyAny()) ensureSentinel();
    renderPlan();
  }

  // ---- bozze
  function onDirty(id, state) {
    if (disposed) return;
    // Un errore del server decade quando il campo cambia valore: il nuovo valore va verificato di nuovo.
    const prev = drafts[id]?.values;
    if (prev && state.values) {
      for (const k of Object.keys(pendingErrors)) {
        if (OWNER[k] === id && prev[k] !== undefined && prev[k] !== state.values[k]) delete pendingErrors[k];
      }
    }
    drafts[id] = state;
    updateBar();
  }

  /** Errori del server destinati alla sezione `id` (gli altri restano in attesa finche' non si cambia scheda). */
  function serverErrorsFor(id) {
    return Object.fromEntries(Object.entries(pendingErrors).filter(([k]) => OWNER[k] === id));
  }

  /** Monta la sezione della scheda attiva con le bozze correnti. Se il modulo manca: "Sezione in arrivo". */
  async function mountActive() {
    const my = ++mountSeq;
    if (section) {
      try { section.api.destroy?.(); } catch (e) { console.error(e); }
      section = null;
    }
    body.replaceChildren(skeletonCard());
    const tab = activeTabs[tabIndex];
    let mod;
    try {
      mod = await import(tab.file);
    } catch {
      if (my !== mountSeq || disposed) return;
      body.replaceChildren(emptyState({ icon: 'construction', title: 'Sezione in arrivo' }));
      return;
    }
    if (my !== mountSeq || disposed) return;
    const container = h('div', { class: `settings-section${tab.wide ? ' settings-section--wide' : ''}` });
    body.replaceChildren(container);
    try {
      const api = await mod.mountSection(container, {
        settings,
        defaults,
        draft: drafts[tab.id]?.values ?? null,
        onDirty: (state) => { if (my === mountSeq) onDirty(tab.id, state); },
        onPlanStale: () => { if (my === mountSeq && !disposed) { planStale = true; planPhase = 'idle'; renderPlan(); } },
      });
      if (my !== mountSeq || disposed) { api?.destroy?.(); return; }
      section = { id: tab.id, api };
      const mine = serverErrorsFor(tab.id);
      if (Object.keys(mine).length) api.setServerErrors?.(mine);
      if (revealNext) { revealNext = false; api.reveal?.(); }
    } catch (e) {
      console.error(e);
      if (my !== mountSeq || disposed) return;
      body.replaceChildren(errorState({ title: 'Questa sezione non si apre', message: e.message }));
    }
  }

  /** Cambia scheda senza perdere le bozze di nessuna sezione. */
  function goTo(id) {
    const idx = activeTabs.findIndex((t) => t.id === id);
    if (idx < 0 || disposed) return;
    tabIndex = idx;
    tabsEl.activeTabIndex = idx;
    selfHash = id !== 'general' ? `#/settings?tab=${id}` : '#/settings';
    // Con modifiche non salvate l'URL resta quello della voce segnaposto (vedi ensureSentinel).
    if (!sentinel) history.replaceState(null, '', selfHash);
    mountActive();
  }

  /** PUT /settings con la patch unita. In caso di errore di validazione porta l'utente sul campo. */
  async function save() {
    if (saving || disposed) return;
    const bad = invalidIds();
    if (bad.length) {
      revealNext = true;
      goTo(bad[0]);
      showSnackbar('Correggi i campi evidenziati prima di salvare.', { error: true });
      return;
    }
    const patch = mergedPatch();
    if (!Object.keys(patch).length) return;
    const sentSnapshot = JSON.stringify(drafts);
    saving = true;
    pendingErrors = {};
    updateBar();
    try {
      const next = await api.put('/settings', patch);
      if (disposed) return;
      if (Object.keys(patch).some((k) => PLAN_KEYS.has(k))) { planStale = true; planPhase = 'idle'; }
      settings = next;
      store.set('settings', next);
      // Via solo le bozze inviate e rimaste invariate: quelle modificate durante il salvataggio restano.
      const sent = JSON.parse(sentSnapshot);
      for (const id of Object.keys(drafts)) {
        if (JSON.stringify(drafts[id]) === JSON.stringify(sent[id])) delete drafts[id];
      }
      showSnackbar('Impostazioni salvate.');
      await mountActive();
    } catch (e) {
      if (disposed) return;
      const fields = e?.body?.fields && typeof e.body.fields === 'object' ? e.body.fields : {};
      pendingErrors = fields;
      showSnackbar(saveErrorText(e), { error: true, duration: 6000 });
      const first = Object.keys(fields).find((k) => OWNER[k]);
      if (first && OWNER[first] !== activeTabs[tabIndex].id) goTo(OWNER[first]);
      else if (section) section.api.setServerErrors?.(serverErrorsFor(section.id));
    } finally {
      if (!disposed) { saving = false; updateBar(); }
    }
  }

  /** Annulla tutte le bozze e ripristina i valori salvati. */
  function discard() {
    if (saving || disposed) return;
    drafts = {};
    pendingErrors = {};
    updateBar();
    mountActive();
  }

  // ---- piano delle cartelle
  function renderPlan() {
    // Le funzioni legate al piano/cartelle proposte non sono esposte nell'interfaccia corrente.
    planHost.hidden = true;
    planHost.replaceChildren();
  }

  async function rebuild() {
    if (planPhase === 'running' || dirtyAny() || disposed) return;
    planPhase = 'running';
    progress = { done: 0, total: 0, message: 'Avvio del ricalcolo…' };
    renderPlan();
    try {
      const res = await api.post('/plan/rebuild', {});
      if (disposed) return;
      jobId = res.job_id;
      // Il job puo' essere gia' finito prima che arrivi l'evento SSE: controlla lo stato.
      const job = await api.get(`/jobs/${jobId}`).catch(() => null);
      if (job) onJob(job);
    } catch (e) {
      if (disposed) return;
      jobId = null;
      planPhase = 'error';
      planMsg = e.message || 'Impossibile avviare il ricalcolo.';
      renderPlan();
    }
  }

  function onJob(job) {
    if (disposed || !jobId || job.id !== jobId) return;
    if (job.state === 'running') {
      progress = { done: job.done || 0, total: job.total || 0, message: job.message || job.stage || 'Ricalcolo in corso…' };
      renderPlan();
      return;
    }
    jobId = null;
    if (job.state === 'done') {
      planStale = false;
      planPhase = 'idle';
      renderPlan();
      showSnackbar('Piano ricalcolato.', { actionLabel: 'Vedi le cartelle', onAction: () => navigate?.('/folders') });
    } else {
      planPhase = 'error';
      planMsg = job.state === 'cancelled' ? 'Ricalcolo annullato.' : (job.error || 'Il ricalcolo si è interrotto.');
      renderPlan();
    }
  }

  // ---- caricamento e reazione a cambi esterni dello stato globale
  async function load() {
    body.replaceChildren(skeletonCard());
    try {
      const [s, d] = await Promise.all([api.get('/settings'), api.get('/settings/defaults').catch(() => null)]);
      if (disposed) return;
      settings = s;
      defaults = d;
      store.set('settings', s);
      mountActive();
    } catch (e) {
      if (disposed) return;
      body.replaceChildren(errorState({ message: e.message, onRetry: load }));
    }
  }

  // ---- scheda: cambio con il mouse/tastiera (le bozze restano)
  tabsEl.addEventListener('change', () => {
    const idx = tabsEl.activeTabIndex;
    if (idx !== tabIndex && activeTabs[idx]) goTo(activeTabs[idx].id);
  });

  // ---- uscita dalla pagina con modifiche non salvate.
  // - Link della navigazione: il clic in fase di cattura chiede conferma e poi ripete il clic (onDocClick).
  // - Indietro/avanti: il router reagisce a hashchange e non si puo' annullare. Per questo, con bozze sporche,
  //   si inserisce una voce di cronologia "segnaposto" (stesso URL): Indietro la lascia senza cambiare hash
  //   (solo popstate), si chiede conferma e, se si resta, avanti torna sul segnaposto.
  let sentinel = false;
  function ensureSentinel() {
    if (sentinel || disposed) return;
    history.pushState({ fsSettingsGuard: true }, '', location.href);
    sentinel = true;
  }
  /** Toglie la voce segnaposto dalla cronologia (prima di uscire con un link, che poi la sostituisce). */
  function dropSentinel() {
    if (!sentinel) return Promise.resolve();
    sentinel = false;
    return new Promise((resolve) => {
      const done = () => { clearTimeout(timer); window.removeEventListener('popstate', done); resolve(); };
      const timer = setTimeout(done, 300);
      window.addEventListener('popstate', done);
      history.back();
    });
  }
  function onPopState(e) {
    if (disposed || !sentinel) return;
    // Si sta uscendo verso un'altra pagina (anche un clic di navigazione genera popstate): nulla da fare qui.
    if (!location.hash.startsWith('#/settings')) { sentinel = false; return; }
    if (e.state && e.state.fsSettingsGuard) return;
    // Indietro dal segnaposto: siamo sulla voce delle impostazioni sotto di lui (stesso URL, nessun render).
    sentinel = false;
    if (!dirtyAny()) { history.back(); return; }
    confirmLeave().then((ok) => {
      if (disposed) return;
      if (ok) {
        drafts = {};
        pendingErrors = {};
        history.back();                 // voce precedente: il router rende la pagina scelta
      } else {
        sentinel = true;
        history.forward();              // torna sul segnaposto
      }
    });
  }
  window.addEventListener('popstate', onPopState);

  function confirmLeave() {
    return confirmDialog({
      title: 'Modifiche non salvate',
      message: 'Se lasci questa pagina le modifiche non salvate andranno perse.',
      confirmLabel: 'Lascia la pagina',
      cancelLabel: 'Resta qui',
      danger: true,
    });
  }

  // Clic su un link di navigazione con modifiche sporche: conferma, poi ripete il clic originale.
  let bypass = false;
  function onDocClick(e) {
    if (bypass || disposed || !dirtyAny()) return;
    const t = e.target;
    if (!(t instanceof Element) || root.contains(t) || t.closest('.theme-btn')) return;
    if (!t.closest('.app-nav, .app-topbar, a[href^="#/"]')) return;
    e.preventDefault();
    e.stopImmediatePropagation();
    confirmLeave().then(async (ok) => {
      if (!ok || disposed) return;
      drafts = {};
      pendingErrors = {};
      await dropSentinel();
      bypass = true;
      try { t.click(); } finally { bypass = false; }
    });
  }
  document.addEventListener('click', onDocClick, true);

  function onBeforeUnload(e) {
    if (!dirtyAny()) return;
    e.preventDefault();
    e.returnValue = '';
  }
  window.addEventListener('beforeunload', onBeforeUnload);

  // Se le impostazioni cambiano altrove (altra vista), le sezioni si riallineano; le bozze restano.
  const unsubStore = store.subscribe('settings', (v) => {
    if (disposed || v === settings) return;
    settings = v;
    mountActive();
  });
  const unsubJob = onEvent('job', onJob);

  root.append(h('div', { class: 'view settings-view' }, header, tabsEl, body, planHost, bar));
  updateBar();
  load();

  return () => {
    disposed = true;
    mountSeq++;
    window.removeEventListener('popstate', onPopState);
    document.removeEventListener('click', onDocClick, true);
    window.removeEventListener('beforeunload', onBeforeUnload);
    unsubStore();
    unsubJob();
    if (section) { try { section.api.destroy?.(); } catch (e) { console.error(e); } section = null; }
  };
}
