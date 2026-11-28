// Galleria cronologica (stile Google Foto): filtri, griglia virtuale con intestazioni per giorno/mese,
// scrubber laterale, salto rapido a anno/mese, selezione multipla e visualizzatore.
// Route: #/gallery?filter=all|photo|video|favorites|duplicates&year=YYYY&month=M (posizione aggiornata mentre si scorre).
// Ingresso da Statistiche: #/gallery?day=AAAA-MM-GG (posiziona sul giorno, o sul primo giorno precedente con foto).
import { h, icon } from '../ui/el.js';
import { api, waitJob } from '../api.js';
import { showSnackbar } from '../ui/snackbar.js';
import { albumPickerDialog, confirmDialog } from '../ui/dialog.js';
import { skeletonGrid } from '../ui/skeleton.js';
import { emptyState, errorState } from '../ui/empty-state.js';
import { filterChips } from '../ui/chips.js';
import { createSelection, selectionBar, useRangeSelect } from '../ui/selection.js';
import { createTile, updateTile } from '../ui/photo-tile.js';
import { VirtualGrid } from '../ui/virtual-grid.js';
import { createScrubber } from '../ui/scrubber.js';
import { formatBytes, formatDay, formatMonth, formatNumber, plural, mesiNomi, missingNote } from '../ui/format.js';
import { openViewer } from './viewer.js';

const FILTERS = [
  { id: 'all', label: 'Tutte', icon: 'apps' },
  { id: 'photo', label: 'Foto', icon: 'image' },
  { id: 'video', label: 'Video', icon: 'videocam' },
  { id: 'favorites', label: 'Preferiti', icon: 'favorite' },
  { id: 'duplicates', label: 'Duplicati', icon: 'filter_none' },
];
const KIND = { photo: 'image', video: 'video' };
const DENSITY_PX = { 1: 120, 2: 170, 3: 240 };   // tre densita' di miniatura (schermi medi/estesi)
const DENSITY_PX_COMPACT = { 1: 96, 2: 120, 3: 160 }; // telefono: almeno due colonne
const COMPACT_QUERY = '(max-width: 599.98px)';
const PAGE = 120;
const DAY_HEADER_MAX = 2000;   // oltre questo numero di elementi le intestazioni sono per mese
const LONG_PRESS_MS = 450;
const MOVE_TOLERANCE = 10;     // px oltre cui un tocco non e' piu' una pressione prolungata
const ERROR_THROTTLE_MS = 4000;
const CSS_URL = new URL('../../css/view-gallery.css', import.meta.url).href;

const cap = (s) => s.charAt(0).toUpperCase() + s.slice(1);

function ensureCss() {
  if (document.querySelector('link[data-view="gallery"]')) return;
  document.head.append(h('link', { rel: 'stylesheet', href: CSS_URL, dataset: { view: 'gallery' } }));
}

/**
 * Gruppi della griglia dai bucket. Archivio piccolo: un'intestazione per giorno.
 * Oltre DAY_HEADER_MAX elementi: un'intestazione per mese (con un file al giorno i giorni diventano illeggibili).
 */
/** Bucket senza anno: "unknown" = senza data, "uncertain" = data incerta (in coda alla lista). */
const SPECIAL_LABEL = { unknown: 'Senza data', uncertain: 'Data incerta' };
const KEY_RE_DAY = /^\d{4}-\d{2}-\d{2}$/; // bucket giornaliero con data (esclude unknown/uncertain)

function buildGroups(days, months, total) {
  if (total > DAY_HEADER_MAX) {
    return months.map((m) => ({
      key: m.key,
      label: SPECIAL_LABEL[m.key] || cap(formatMonth(m.year, m.month)),
      count: m.count,
      offset: m.offset,
    }));
  }
  return days.map((d) => ({
    key: d.key,
    label: SPECIAL_LABEL[d.key] || cap(formatDay(d.key)),
    count: d.count,
    offset: d.offset,
  }));
}

/** Bucket mensili (per scrubber, menu di salto e URL), derivati dai bucket giornalieri. */
function toMonths(days) {
  const out = [];
  const byKey = new Map();
  for (const d of days) {
    if (d.key === 'unknown' || d.key === 'uncertain') {
      out.push({ key: d.key, year: null, month: null, count: d.count, offset: d.offset });
      continue;
    }
    const key = d.key.slice(0, 7);
    let m = byKey.get(key);
    if (!m) {
      m = { key, year: +key.slice(0, 4), month: +key.slice(5, 7), count: 0, offset: d.offset };
      byKey.set(key, m);
      out.push(m);
    }
    m.count += d.count;
  }
  return out;
}

function renderHeader(group) {
  return h('div', { class: 'gallery-group' },
    h('h2', { class: 'gallery-group-title md-typescale-title-small' }, group.label),
    h('span', { class: 'gallery-group-count md-typescale-label-medium' }, formatNumber(group.count)));
}

export async function mount(root, ctx) {
  ensureCss();
  const query = ctx.query || {};
  const state = {
    filter: FILTERS.some((f) => f.id === query.filter) ? query.filter : 'all',
    personId: query.person && /^\d+$/.test(String(query.person)) ? Number(query.person) : null,
    density: 2,
  };

  let destroyed = false;
  const compactMql = matchMedia(COMPACT_QUERY);
  const tilePx = () => (compactMql.matches ? DENSITY_PX_COMPACT : DENSITY_PX)[state.density];
  let gen = 0;                         // identifica i dati correnti: le risposte di una generazione vecchia vengono scartate
  let genAc = null;                     // AbortController della generazione corrente
  let grid = null;
  let scrubber = null;
  let days = [];
  let months = [];
  let total = 0;
  let firstVisible = 0;
  let anchorId = null;                  // ultimo elemento toccato (per "seleziona tutto del giorno")
  let lastShift = false;                // stato di Shift dell'ultimo click (letto dai tile)
  let lastCtrl = false;                 // stato di Ctrl/Cmd dell'ultimo click (selezione multipla rapida)
  let suppressClick = false;            // click seguito a una pressione prolungata: va ignorato
  let lpTimer = 0;
  let lpStart = null;
  let urlTimer = 0;
  let refreshTimer = 0;
  let lastUrl = '';
  let lastErrorAt = 0;
  const favTimers = new Set();
  // posizione da ripristinare al primo caricamento (da URL: ?day=AAAA-MM-GG oppure ?year=&month=)
  const pendingPos = { day: query.day, year: query.year, month: query.month };
  const slots = new Map();              // offset -> File (caricati)
  const byId = new Map();               // id -> { file, off }

  // ---------- DOM ----------
  const countEl = h('span', { class: 'gallery-count md-typescale-label-large', 'aria-live': 'polite', 'aria-label': 'Numero di elementi' });
  // Pulsante di testo: "· 1.253 duplicati nascosti" (visibile solo se ce ne sono) -> apre il filtro Duplicati.
  const dupNoteEl = h('md-text-button', { class: 'gallery-count-note', title: 'Mostra le copie duplicate', onclick: () => setFilter('duplicates') });
  dupNoteEl.hidden = true;
  // Solo nel filtro Duplicati: spiega a cosa servono le copie e rimanda alla revisione con il confronto.
  const dupHintEl = h('md-text-button', { class: 'gallery-dup-hint', onclick: () => ctx.navigate('/duplicates') }, 'Confronta con gli originali');
  dupHintEl.hidden = state.filter !== 'duplicates';
  // File non utilizzabili (mancanti sul disco o illeggibili): spunta per nasconderli + rimozione dal catalogo.
  const BROKEN_KEY = 'fs.gallery.hideBroken';
  // Storage sempre in try/catch: puo' essere bloccato o pieno.
  const store = (area) => { try { return globalThis[area]; } catch { return null; } };
  const readKey = (area, k) => { try { return store(area)?.getItem(k) ?? null; } catch { return null; } };
  const writeKey = (area, k, v) => { try { store(area)?.setItem(k, v); } catch { /* ignora */ } };
  let hideBroken = readKey('localStorage', BROKEN_KEY) !== '0';   // predefinito: nascosti
  const brokenCheck = h('md-checkbox', { 'touch-target': 'wrapper', 'aria-label': 'Nascondi i file danneggiati' });
  brokenCheck.checked = hideBroken;
  const brokenLabel = h('span', { class: 'gallery-broken__label' }, 'Nascondi danneggiati');
  const brokenRemove = h('md-text-button', {
    class: 'gallery-broken__remove',
    title: 'Toglie dal catalogo di PhotoScan le voci dei file che non esistono più o non si aprono. Nessun file viene cancellato dal disco.',
    onclick: () => removeBroken(),
  }, 'Rimuovi');
  const brokenEl = h('label', { class: 'gallery-broken', title: 'File che non esistono più sul disco o che non si riescono ad aprire' },
    brokenCheck, brokenLabel);
  const brokenWrap = h('div', { class: 'gallery-broken-wrap' }, brokenEl, brokenRemove);
  brokenWrap.hidden = true;
  brokenCheck.addEventListener('change', () => {
    hideBroken = brokenCheck.checked;
    writeKey('localStorage', BROKEN_KEY, hideBroken ? '1' : '0');
    selection.clear();
    load();
  });
  const personBadgeLabel = h('span', {});
  const personBadge = h('md-input-chip', {
    hidden: !state.personId,
    removeOnly: false,
    title: 'Rimuovi filtro persona',
  });
  personBadge.label = state.personId ? 'Persona selezionata' : '';
  personBadge.append(icon('face', { slot: 'icon' }));
  personBadge.addEventListener('click', () => setPerson(null));
  personBadge.addEventListener('remove', () => setPerson(null));

  const personBtn = h('md-icon-button', {
    hidden: true,
    'aria-label': 'Filtra per persona',
    title: 'Filtra per persona',
    'aria-haspopup': 'menu',
    'aria-expanded': 'false',
  }, icon('face'));
  const personMenu = h('md-menu', { positioning: 'popover', class: 'gallery-jump-menu' });
  personMenu.anchorElement = personBtn;
  const onPersonBtnClick = () => { personMenu.open = !personMenu.open; };
  personBtn.addEventListener('click', onPersonBtnClick);

  const jumpBtn = h('md-icon-button', { 'aria-label': 'Vai a un anno o un mese', title: 'Vai a un anno o un mese', 'aria-haspopup': 'dialog', 'aria-expanded': 'false' }, icon('calendar_month'));
  const jumpPopover = h('div', { class: 'gallery-jump-popover', role: 'dialog', 'aria-label': 'Vai a un anno o un mese', hidden: true });
  const jumpWrap = h('div', { class: 'gallery-jump-wrap' }, jumpBtn, jumpPopover);
  let jumpOpen = false;
  let jumpSelectedYear = null;

  function setJumpOpen(on) {
    jumpOpen = !!on;
    jumpPopover.hidden = !jumpOpen;
    jumpBtn.setAttribute('aria-expanded', String(jumpOpen));
    if (jumpOpen) renderJumpMenu();
  }
  const onJumpClick = (e) => { e.stopPropagation(); setJumpOpen(!jumpOpen); };
  const onJumpDocClick = (e) => {
    if (jumpOpen && !jumpWrap.contains(e.target)) setJumpOpen(false);
  };
  const onJumpKey = (e) => {
    if (jumpOpen && e.key === 'Escape') { e.preventDefault(); setJumpOpen(false); jumpBtn.focus(); }
  };
  jumpBtn.addEventListener('click', onJumpClick);
  document.addEventListener('click', onJumpDocClick);
  document.addEventListener('keydown', onJumpKey);
  const densityIcon = icon('apps');       // solo etichetta visiva dello slider: non interattiva
  densityIcon.setAttribute('aria-hidden', 'true');
  // estremo destro dello slider (miniature grandi): decorativo, come densityIcon a sinistra
  const selectIcon = icon('crop_square');
  selectIcon.setAttribute('aria-hidden', 'true');
  const slider = h('md-slider', { min: 1, max: 3, step: 1, value: state.density, title: 'Dimensione miniature', 'aria-label': 'Dimensione miniature' });
  const chips = filterChips(FILTERS, { selected: state.filter, label: 'Tipo di contenuto', onChange: (id) => setFilter(id) });
  const bar = h('div', { class: 'gallery-bar-slot' });
  const body = h('div', { class: 'gallery-body' });
  const scrollEl = h('div', { class: 'gallery-scroll' });
  const railHost = h('div', { class: 'gallery-rail' });
  root.replaceChildren(h('div', { class: 'gallery' },
    h('header', { class: 'gallery-head' },
      h('h1', { class: 'sr-only' }, 'Foto e video'),
      h('div', { class: 'gallery-head-row' },
        chips,
        h('div', { class: 'gallery-tools' },
          personBtn,
          personMenu,
          jumpWrap,
          h('div', { class: 'gallery-density', role: 'group', 'aria-label': 'Dimensione miniature', title: 'Dimensione miniature' },
            densityIcon,
            slider,
            selectIcon))),
      h('div', { class: 'gallery-meta-row' },
        h('div', { class: 'gallery-count-wrap' }, countEl, personBadge, dupNoteEl, dupHintEl),
        brokenWrap)),
    bar,
    body));

  // ---------- selezione ----------
  const selection = createSelection();
  const rangeSelect = useRangeSelect(selection, orderedIds);
  const select = (id, opts = {}) => { anchorId = id; rangeSelect(id, opts); };
  // label = nome accessibile e tooltip; text = etichetta visibile (solo da 600px, vedi selection.js). "Annulla" e' gia' la X della barra.
  const actions = [
    { id: 'favorite', icon: 'favorite', label: 'Aggiungi ai preferiti', text: 'Preferito', run: toggleFavoriteSelection },
    { id: 'album', icon: 'photo_album', label: 'Aggiungi ad album', text: 'Album', run: addToAlbum },
    { id: 'day', icon: 'calendar_today', label: 'Seleziona tutto il giorno', text: 'Tutto il giorno', run: selectWholeDay },
    { id: 'trash', icon: 'delete', label: 'Sposta nel Cestino', text: 'Cestino', run: trashSelection },
  ];
  const bulkBar = selectionBar(bar, { selection, actions });
  const unsubSelection = selection.subscribe(() => syncTiles());

  // ---------- eventi della griglia (cattura: vedono Shift/Ctrl e i click da scartare) ----------
  const onCaptureClick = (e) => {
    if (suppressClick) { suppressClick = false; e.stopPropagation(); e.preventDefault(); return; }
    lastShift = e.shiftKey;
    lastCtrl = e.ctrlKey || e.metaKey;
    const fav = e.target.closest?.('.photo-tile__fav');
    if (fav) scheduleFavSync(fav.closest('.photo-tile'));
  };
  const onCaptureKey = (e) => {
    // L'apertura da tastiera non e' un click: lo stato dei modificatori non deve restare quello dell'ultimo clic.
    lastShift = e.shiftKey;
    lastCtrl = e.ctrlKey || e.metaKey;
    if (e.key === 'f' || e.key === 'F') scheduleFavSync(e.target.closest?.('.photo-tile'));
    if (e.key === 'Delete' && selection.count > 0) { e.preventDefault(); trashSelection(); }
  };
  let ctrlDrag = null; // { anchorId, baseIds: Set, moved: boolean } per trascinamento con Ctrl premuto

  function applyCtrlRange(targetId) {
    if (!ctrlDrag) return;
    const ids = orderedIds();
    const a = ids.indexOf(ctrlDrag.anchorId);
    const b = ids.indexOf(targetId);
    if (a < 0 || b < 0) return;
    const [lo, hi] = a <= b ? [a, b] : [b, a];
    const next = new Set(ctrlDrag.baseIds);
    for (let i = lo; i <= hi; i++) next.add(ids[i]);
    selection.selectAll([...next]);
  }

  const onPointerDown = (e) => {
    suppressClick = false;
    cancelLongPress();
    ctrlDrag = null;
    const tile = e.target.closest?.('.photo-tile');
    if (!tile || e.target.closest('.photo-tile__fav, .photo-tile__check')) return;
    const id = Number(tile.dataset.id);
    // Ctrl + trascinamento con mouse: seleziona tutte le foto dalla prima all'ultima toccata
    if (e.pointerType === 'mouse' && e.button === 0 && (e.ctrlKey || e.metaKey)) {
      e.preventDefault();
      ctrlDrag = { anchorId: id, baseIds: new Set(selection.ids), moved: false };
      anchorId = id;
      return;
    }
    // solo tocco/penna: con il mouse la pressione lunga e' un click normale (non deve selezionare)
    if (e.pointerType === 'mouse' || e.button !== 0) return;
    lpStart = { x: e.clientX, y: e.clientY };
    lpTimer = setTimeout(() => {
      lpTimer = 0;
      suppressClick = true;
      if (!selection.has(id)) select(id);
    }, LONG_PRESS_MS);
  };
  const onPointerMove = (e) => {
    if (ctrlDrag) {
      const tile = document.elementFromPoint(e.clientX, e.clientY)?.closest?.('.photo-tile');
      if (tile) {
        const id = Number(tile.dataset.id);
        if (id !== ctrlDrag.anchorId) ctrlDrag.moved = true;
        applyCtrlRange(id);
      }
      return;
    }
    if (lpTimer && lpStart && Math.hypot(e.clientX - lpStart.x, e.clientY - lpStart.y) > MOVE_TOLERANCE) cancelLongPress();
  };
  const onPointerUp = () => {
    cancelLongPress();
    if (ctrlDrag?.moved) suppressClick = true;
    ctrlDrag = null;
  };
  const onGridError = (e) => reportError(e.detail?.error);
  scrollEl.addEventListener('click', onCaptureClick, true);
  scrollEl.addEventListener('keydown', onCaptureKey, true);
  scrollEl.addEventListener('pointerdown', onPointerDown, true);
  scrollEl.addEventListener('pointermove', onPointerMove);
  scrollEl.addEventListener('pointerup', onPointerUp);
  scrollEl.addEventListener('pointercancel', onPointerUp);
  scrollEl.addEventListener('error', onGridError);

  const onSliderChange = () => {
    const d = Number(slider.value);
    if (!d || d === state.density) return;
    state.density = d;
    grid?.setTileSize(tilePx());
  };
  slider.addEventListener('change', onSliderChange);
  const onCompactChange = () => {
    // su telefono lo slider e' nascosto: si torna alla densita' predefinita
    if (compactMql.matches && state.density !== 2) { state.density = 2; slider.value = 2; }
    grid?.setTileSize(tilePx());
  };
  compactMql.addEventListener('change', onCompactChange);

  const onAbort = () => genAc?.abort();
  ctx.signal?.addEventListener('abort', onAbort);

  // ---------- dati ----------
  function listParams() {
    // 'duplicates': solo le copie duplicate (nessun filtro di tipo); altrove le copie restano nascoste
    return {
      kind: KIND[state.filter], favorite: state.filter === 'favorites' ? 1 : undefined,
      dup: state.filter === 'duplicates' ? 'only' : 'hide', broken: hideBroken ? 'hide' : undefined,
      person_id: state.personId || undefined,
    };
  }

  let peopleList = [];
  function syncPersonBadge() {
    if (!state.personId) {
      personBadge.hidden = true;
      return;
    }
    const found = peopleList.find((p) => p.id === state.personId);
    personBadge.label = `${found?.name || 'Persona'} ×`;
    personBadge.hidden = false;
  }

  function setPerson(id) {
    if (id === state.personId || destroyed) return;
    state.personId = id;
    syncPersonBadge();
    pendingPos.day = pendingPos.year = pendingPos.month = undefined;
    firstVisible = 0;
    selection.clear();
    writeUrl();
    load();
  }

  async function loadPeopleFilter() {
    try {
      const list = await api.get('/people', { include_hidden: 1 }, ctx.signal);
      if (destroyed || !Array.isArray(list)) return;
      peopleList = list;
      syncPersonBadge();
      const visible = list.filter((p) => !p.hidden);
      personBtn.hidden = visible.length === 0;
      const items = [
        h('md-menu-item', { onclick: () => setPerson(null) },
          h('div', { slot: 'headline' }, 'Tutte le persone')),
        ...(visible.length ? [h('md-divider', { role: 'separator' })] : []),
        ...visible.map((p) => h('md-menu-item', { onclick: () => setPerson(p.id) },
          h('div', { slot: 'headline' }, p.name || 'Senza nome'),
          h('div', { slot: 'supporting-text' }, plural(p.count, 'foto', 'foto')))),
      ];
      personMenu.replaceChildren(...items);
    } catch {
      personBtn.hidden = true;
    }
  }

  /** Riepilogo dei file danneggiati. Una volta per sessione controlla anche il disco (solo lettura). */
  let brokenCount = 0;
  async function refreshBroken({ check = false } = {}) {
    try {
      const doCheck = check || !readKey('sessionStorage', 'fs.brokenChecked');
      let r;
      if (doCheck) {
        const job = await waitJob(await api.post('/library/broken/check'));
        r = job?.state === 'done' && job.result ? job.result : await api.get('/library/broken');
      } else {
        r = await api.get('/library/broken');
      }
      if (doCheck) writeKey('sessionStorage', 'fs.brokenChecked', '1');
      if (destroyed) return;
      const before = brokenCount;
      brokenCount = r.total || 0;
      brokenLabel.textContent = `Nascondi danneggiati (${formatNumber(brokenCount)})`;
      brokenWrap.hidden = brokenCount === 0;
      if (doCheck && hideBroken && brokenCount !== before) load();   // nuovi file scoperti: toglili dalla griglia
    } catch {
      brokenWrap.hidden = brokenCount === 0;
    }
  }

  async function removeBroken() {
    if (!brokenCount) return;
    const ok = await confirmDialog({
      title: `Rimuovere ${plural(brokenCount, 'voce', 'voci')} dal catalogo?`,
      message: 'Sono file che non esistono più sul disco o che non si riescono ad aprire. PhotoScan toglie solo le loro voci dal proprio elenco: nessun file viene cancellato dal disco. Se un file ricompare, verrà ritrovato alla prossima analisi.',
      confirmLabel: 'Rimuovi dal catalogo',
      danger: true,
    });
    if (!ok || destroyed) return;
    try {
      const r = await api.post('/library/broken/remove', { confirm: true });
      showSnackbar(`${plural(r.removed, 'voce rimossa', 'voci rimosse')} dal catalogo`);
      brokenCount = 0;
      brokenWrap.hidden = true;
      selection.clear();
      load();
    } catch (e) {
      showSnackbar(e?.message || 'Rimozione non riuscita', { error: true });
    }
  }

  /** Id tolti dal filtro Preferiti (deselezionati): vanno tolti anche dalla selezione al prossimo ricaricamento. */
  const removedFav = new Set();
  function pruneRemoved() {
    if (!removedFav.size) return;
    selection.selectAll([...selection.ids].filter((id) => !removedFav.has(id)));
    removedFav.clear();
  }

  /** Conteggio dei duplicati nascosti (per il pulsante "· N duplicati nascosti"): una sola riga, niente dati. */
  async function refreshDupNote() {
    const myGen = gen;
    if (state.filter === 'duplicates') { dupNoteEl.hidden = true; return; }
    try {
      const page = await api.get('/files', { kind: KIND[state.filter], dup: 'only', offset: 0, limit: 1 });
      if (destroyed || myGen !== gen) return;
      const n = page.total || 0;
      dupNoteEl.textContent = `· ${formatNumber(n)} duplicati`;
      dupNoteEl.hidden = n === 0;
    } catch {
      dupNoteEl.hidden = true;
    }
  }

  function reportError(err) {
    if (destroyed || err?.name === 'AbortError') return;
    const now = Date.now();
    if (now - lastErrorAt < ERROR_THROTTLE_MS) return;
    lastErrorAt = now;
    showSnackbar(err?.message || 'Impossibile caricare le foto', { error: true });
  }

  function storePage(offset, items) {
    items.forEach((f, i) => {
      slots.set(offset + i, f);
      byId.set(f.id, { file: f, off: offset + i });
    });
  }

  function resetData() {
    gen++;
    genAc?.abort();
    genAc = new AbortController();
    slots.clear();
    byId.clear();
  }

  /** Pagina per la griglia virtuale. Gli errori arrivano all'utente tramite l'evento 'error' della griglia. */
  async function fetchPage(offset, limit, signal) {
    const myGen = gen;
    const page = await api.get('/files', { ...listParams(), offset, limit, sort: 'date_desc' }, signal);
    if (destroyed || myGen !== gen) return { total, items: [] };
    total = page.total;
    storePage(offset, page.items);
    return page;
  }

  /**
   * Carica i bucket e ricostruisce la griglia. `silent` (aggiornamento dopo un preferito): la griglia
   * attuale resta a schermo fino ai nuovi dati, niente scheletro.
   */
  async function load({ keep = null, silent = false } = {}) {
    pruneRemoved();
    resetData();
    const myGen = gen;
    const sig = genAc.signal;
    if (!silent) {
      scrubber?.destroy();
      scrubber = null;
      grid?.destroy();
      grid = null;
      scrollEl.replaceChildren();
      railHost.replaceChildren();
      body.replaceChildren(h('div', { class: 'gallery-skeleton' }, skeletonGrid(24, { tile: tilePx() })));
      countEl.textContent = '';
    }
    try {
      const res = await api.get('/timeline/buckets', { ...listParams(), granularity: 'day' }, sig);
      if (destroyed || myGen !== gen) return;
      days = res.buckets || [];
      total = res.total || 0;
      months = toMonths(days);
      countEl.textContent = state.filter === 'duplicates' ? plural(total, 'duplicato', 'duplicati') : plural(total, 'elemento', 'elementi');
      dupHintEl.hidden = state.filter !== 'duplicates';
      refreshDupNote();
      renderJumpMenu();
      jumpBtn.disabled = !months.length;
      if (!total) { showEmpty(); return; }
      buildGrid(keep ?? initialOffset() ?? 0);
    } catch (err) {
      if (destroyed || myGen !== gen || err?.name === 'AbortError') return;
      body.replaceChildren(errorState({
        title: 'Impossibile caricare la galleria',
        message: err?.message,
        onRetry: () => load({ keep }),
      }));
    }
  }

  /** Offset del mese richiesto dall'URL (year/month), una sola volta. */
  function initialOffset() {
    const { day, year, month } = pendingPos;
    pendingPos.day = pendingPos.year = pendingPos.month = undefined;
    if (day && /^\d{4}-\d{2}-\d{2}$/.test(day)) {
      // giorno: esatto, oppure il primo giorno con foto non successivo (la lista e' in ordine decrescente)
      const hit = days.find((d) => KEY_RE_DAY.test(d.key) && d.key <= day);
      if (hit) return hit.offset;
    }
    if (!year) return null;
    const hit = months.find((m) => m.year === +year && (!month || m.month === +month));
    return hit ? hit.offset : null;
  }

  function scheduleRefresh() {
    clearTimeout(refreshTimer);
    refreshTimer = setTimeout(() => { if (!destroyed) load({ keep: firstVisible, silent: true }); }, 500);
  }

  // ---------- griglia ----------
  const scrollRatio = () => {
    const max = scrollEl.scrollHeight - scrollEl.clientHeight;
    return max > 0 ? scrollEl.scrollTop / max : 0;
  };

  function buildGrid(startOffset) {
    if (destroyed || !total) return;
    scrubber?.destroy();
    grid?.destroy();
    scrollEl.replaceChildren();
    railHost.replaceChildren();
    body.replaceChildren(scrollEl, railHost);
    scrubber = createScrubber(railHost, {
      buckets: months,
      onSeek: (off) => grid?.scrollToOffset(off),
      getScrollRatio: scrollRatio,
    });
    const g = new VirtualGrid(scrollEl, {
      fetchPage,
      renderItem,
      tile: tilePx(),
      gap: 4,
      pageSize: PAGE,
      groups: buildGroups(days, months, total),
      renderHeader,
      onVisibleRange: onVisible,
    });
    grid = g;
    firstVisible = startOffset;
    g.reload().then(() => {
      if (!destroyed && grid === g && startOffset) g.scrollToOffset(startOffset);
    });
  }

  function onVisible(first) {
    firstVisible = first;
    scrubber?.update(first);
    clearTimeout(urlTimer);
    urlTimer = setTimeout(writeUrl, 300);
  }

  /** Aggiorna year/month nell'hash senza ricaricare la view (replaceState non scatena hashchange). */
  function writeUrl() {
    if (destroyed) return;
    let cur = null;
    for (const m of months) {
      if (m.year == null) continue; // senza data / data incerta: nessun anno nell'URL
      if (m.offset <= firstVisible) cur = m; else break;
    }
    const p = new URLSearchParams({ filter: state.filter });
    if (state.personId) p.set('person', String(state.personId));
    if (cur) { p.set('year', String(cur.year)); p.set('month', String(cur.month)); }
    const url = `#/gallery?${p}`;
    if (url !== lastUrl) {
      lastUrl = url;
      history.replaceState(null, '', url);
    }
  }

  function renderItem(file, index) {
    const tile = createTile(file, {
      size: tilePx(),
      selectable: selection.count > 0,
      selected: selection.has(file.id),
      onOpen: (f) => onOpen(f, index),
      onSelectToggle: (f) => select(f.id, { shiftKey: lastShift }),
    });
    tile.dataset.id = String(file.id);
    return tile;
  }

  /** Allinea modalita' selezione e spunte di tutte le tile montate (solo quelle visibili). */
  function syncTiles() {
    if (destroyed) return;
    const on = selection.count > 0;
    for (const tile of scrollEl.querySelectorAll('.photo-tile')) {
      updateTile(tile, { selectable: on, selected: selection.has(Number(tile.dataset.id)) });
    }
  }

  function orderedIds() {
    const out = [];
    for (let i = 0; i < total; i++) {
      const f = slots.get(i);
      if (f) out.push(f.id);
    }
    return out;
  }

  function cancelLongPress() {
    if (lpTimer) clearTimeout(lpTimer);
    lpTimer = 0;
    lpStart = null;
  }

  function onOpen(file, index) {
    if (selection.count > 0 || lastShift || lastCtrl) {
      // Tenendo premuto Ctrl (o Shift) da una foto a un'altra seleziona tutte le foto comprese nell'intervallo.
      select(file.id, { shiftKey: lastShift || (lastCtrl && selection.count > 0) });
      return;
    }
    openViewerAt(byId.get(file.id)?.off ?? index);
  }

  /** Apre il visualizzatore con la sequenza contigua di elementi caricati attorno a `off`. */
  function openViewerAt(off) {
    let start = off;
    while (start > 0 && slots.has(start - 1)) start--;
    let end = off;
    while (slots.has(end + 1)) end++;
    const items = [];
    for (let i = start; i <= end; i++) items.push(slots.get(i));
    let cursor = end + 1;
    const myGen = gen;
    openViewer({
      items,
      index: off - start,
      loadMore: async () => {
        if (destroyed || myGen !== gen || cursor >= total) return [];
        const from = cursor;
        try {
          const page = await api.get('/files', { ...listParams(), offset: from, limit: PAGE, sort: 'date_desc' });
          if (destroyed || myGen !== gen) return [];
          storePage(from, page.items);
          cursor = from + page.items.length;
          return page.items;
        } catch (err) {
          reportError(err);
          return [];
        }
      },
      onChange: (file) => {
        setFav(file.id, !!file.favorite);
        if (state.filter === 'favorites' && !file.favorite) scheduleRefresh();
      },
      onDelete: () => {
        scheduleRefresh();
      },
    });
  }

  function scheduleFavSync(tile) {
    if (!tile) return;
    const t = setTimeout(() => {
      favTimers.delete(t);
      if (destroyed || !tile.isConnected || !tile._ft) return;
      const id = Number(tile.dataset.id);
      const fav = !!tile._ft.file.favorite;
      if (!!byId.get(id)?.file.favorite === fav) return;
      setFav(id, fav);
      if (state.filter === 'favorites' && !fav) scheduleRefresh();
    }, 700);
    favTimers.add(t);
  }

  /** Aggiorna il preferito nei dati e nelle sole tile montate (nessun rerender della griglia). */
  function setFav(id, value) {
    const entry = byId.get(id);
    if (entry && !!entry.file.favorite !== value) {
      entry.file = { ...entry.file, favorite: value };
      slots.set(entry.off, entry.file);
    }
    if (state.filter === 'favorites' && !value) removedFav.add(id);
    else removedFav.delete(id);
    if (destroyed) return;
    grid?.patchItem?.(id, { favorite: value });
    for (const tile of scrollEl.querySelectorAll(`.photo-tile[data-id="${id}"]`)) {
      updateTile(tile, { favorite: value });
    }
  }

  // ---------- azioni sulla selezione ----------
  async function toggleFavoriteSelection() {
    const ids = [...selection.ids];
    if (!ids.length) return;
    const value = !ids.every((id) => byId.get(id)?.file.favorite);
    try {
      await api.post('/files/favorite', { ids, value });
    } catch (err) {
      showSnackbar(err?.message || 'Impossibile aggiornare i preferiti', { error: true });
      return;
    }
    if (destroyed) return;
    ids.forEach((id) => setFav(id, value));
    if (state.filter === 'favorites' && !value) scheduleRefresh();
    showSnackbar(value ? 'Aggiunti ai preferiti' : 'Rimossi dai preferiti');
  }

  async function addToAlbum() {
    const fileIds = [...selection.ids];
    if (!fileIds.length) return;
    try {
      await albumPickerDialog({ fileIds });
    } catch (err) {
      showSnackbar(err?.message || 'Impossibile aggiungere all\'album', { error: true });
    }
  }

  async function selectWholeDay() {
    const date = anchorId != null ? byId.get(anchorId)?.file.date : null;
    if (!date) {
      showSnackbar(anchorId == null
        ? 'Seleziona prima una foto'
        : 'Questa foto non ha una data: non si può selezionare il suo giorno', { error: true });
      return;
    }
    const myGen = gen;
    try {
      // 50000 = massimo dell'endpoint: un giorno con molte foto non viene tagliato
      const res = await api.get('/files/ids', { ...listParams(), day: date.slice(0, 10), limit: 50000 });
      if (destroyed || myGen !== gen) return;
      selection.selectAll([...selection.ids, ...res.ids]);
    } catch (err) {
      showSnackbar(err?.message || 'Impossibile selezionare il giorno', { error: true });
    }
  }

  let trashing = false;   // evita una seconda richiesta (tasto Canc/pulsante) mentre la prima e' in corso
  async function trashSelection() {
    const ids = [...selection.ids];
    if (!ids.length || trashing) return;
    trashing = true;
    try {
      await trashSelectionNow(ids);
    } finally {
      trashing = false;
    }
  }

  async function trashSelectionNow(ids) {
    const ok = await confirmDialog({
      title: `Spostare ${plural(ids.length, 'elemento', 'elementi')} nel Cestino?`,
      message: 'I file selezionati verranno spostati nel Cestino di Windows e rimossi dal catalogo. Potrai sempre recuperarli dal Cestino.',
      confirmLabel: 'Sposta nel Cestino',
      danger: true,
    });
    if (!ok || destroyed) return;
    try {
      const res = await api.post('/cleanup/trash', { file_ids: ids, confirm: true });
      if (destroyed) return;
      selection.clear();
      showSnackbar(`${plural(res.trashed || ids.length, 'elemento spostato', 'elementi spostati')} nel Cestino (${formatBytes(res.freed_bytes || 0)} liberati).`);
      if (res.missing) showSnackbar(missingNote(res.missing));
      load({ keep: firstVisible, silent: true });
    } catch (err) {
      if (!destroyed) showSnackbar(err?.message || 'Impossibile spostare nel Cestino', { error: true });
    }
  }

  // ---------- filtro, salto, stati ----------
  function setFilter(id) {
    if (id === state.filter || destroyed) return;
    state.filter = id;
    chips.setSelected(id); // i chip vanno aggiornati anche quando il filtro cambia da codice ("Mostra tutto")
    pendingPos.day = pendingPos.year = pendingPos.month = undefined;
    firstVisible = 0;
    removedFav.clear();
    selection.clear();
    writeUrl();
    load();
  }

  function jumpTo(offset) {
    setJumpOpen(false);
    grid?.scrollToOffset(offset);
  }

  const MESI_SHORT = ['Gen', 'Feb', 'Mar', 'Apr', 'Mag', 'Giu', 'Lug', 'Ago', 'Set', 'Ott', 'Nov', 'Dic'];

  function renderJumpMenu() {
    const byYear = new Map(); // year -> { total, firstOffset, months: Map<1..12, {count, offset}> }
    const specials = [];
    for (const m of months) {
      if (m.year == null) { specials.push(m); continue; }
      let entry = byYear.get(m.year);
      if (!entry) {
        entry = { year: m.year, total: 0, firstOffset: m.offset, months: new Map() };
        byYear.set(m.year, entry);
      }
      entry.total += m.count;
      entry.months.set(m.month, { count: m.count, offset: m.offset });
    }
    const years = [...byYear.keys()].sort((a, b) => b - a);
    if (!years.length && !specials.length) {
      jumpPopover.replaceChildren();
      return;
    }
    if (!jumpSelectedYear || !byYear.has(jumpSelectedYear)) {
      jumpSelectedYear = years[0] ?? null;
    }

    const parts = [];
    if (years.length) {
      const yr = byYear.get(jumpSelectedYear);
      const yearStrip = h('div', { class: 'gallery-jump-years', role: 'tablist', 'aria-label': 'Anni' },
        ...years.map((y) => h('button', {
          type: 'button',
          role: 'tab',
          'aria-selected': String(y === jumpSelectedYear),
          class: `gallery-jump-year-pill${y === jumpSelectedYear ? ' is-active' : ''}`,
          onclick: () => { jumpSelectedYear = y; renderJumpMenu(); },
        }, String(y))));

      const monthGrid = h('div', { class: 'gallery-jump-months' },
        ...MESI_SHORT.map((lbl, idx) => {
          const info = yr?.months.get(idx + 1);
          return h('button', {
            type: 'button',
            class: `gallery-jump-month${info ? '' : ' is-empty'}`,
            disabled: !info,
            title: info ? `${cap(mesiNomi[idx])} ${jumpSelectedYear}: ${plural(info.count, 'elemento', 'elementi')}` : 'Nessun elemento',
            onclick: () => { if (info) jumpTo(info.offset); },
          },
            h('span', { class: 'gallery-jump-month__name' }, lbl),
            h('span', { class: 'gallery-jump-month__count' }, info ? formatNumber(info.count) : '—'));
        }));

      const head = h('div', { class: 'gallery-jump-head' },
        h('span', { class: 'md-typescale-title-small' }, `${jumpSelectedYear} · ${plural(yr.total, 'elemento', 'elementi')}`),
        h('md-text-button', { onclick: () => jumpTo(yr.firstOffset) }, `Vai al ${jumpSelectedYear}`));

      parts.push(yearStrip, head, monthGrid);
    }

    if (specials.length) {
      parts.push(h('div', { class: 'gallery-jump-specials' },
        ...specials.map((s) => h('button', {
          type: 'button', class: 'gallery-jump-special',
          onclick: () => jumpTo(s.offset),
        }, `${SPECIAL_LABEL[s.key] || 'Senza data'} (${formatNumber(s.count)})`))));
    }

    jumpPopover.replaceChildren(...parts);
  }

  // Testo per filtro: spiega come riempire la vista (utenti non tecnici).
  const EMPTY_TEXT = {
    photo: { title: 'Nessuna foto', message: 'In questo archivio non ci sono foto.' },
    video: { title: 'Nessun video', message: 'In questo archivio non ci sono video.' },
    favorites: { title: 'Nessun preferito', message: 'Tocca il cuore su una foto per aggiungerla ai preferiti.' },
    duplicates: { title: 'Nessuna copia duplicata', message: 'Non ci sono copie da controllare: tutte le foto sono uniche.' },
  };

  function showEmpty() {
    const isAll = state.filter === 'all';
    const t = EMPTY_TEXT[state.filter];
    body.replaceChildren(emptyState({
      icon: isAll ? 'photo_library' : 'filter_alt_off',
      title: isAll ? 'Non ci sono ancora foto' : t.title,
      message: isAll
        ? 'Avvia l\'indicizzazione della cartella delle foto: le troverai qui, in ordine di data.'
        : t.message,
      actionLabel: isAll ? 'Vai all\'indicizzazione' : 'Mostra tutto',
      onAction: isAll ? () => ctx.navigate('/index') : () => setFilter('all'),
    }));
  }

  // Avvio: primo caricamento.
  load();
  refreshBroken();
  loadPeopleFilter();

  return () => {
    destroyed = true;
    gen++;
    genAc?.abort();
    ctx.signal?.removeEventListener('abort', onAbort);
    clearTimeout(urlTimer);
    clearTimeout(refreshTimer);
    cancelLongPress();
    favTimers.forEach(clearTimeout);
    favTimers.clear();
    scrollEl.removeEventListener('click', onCaptureClick, true);
    scrollEl.removeEventListener('keydown', onCaptureKey, true);
    scrollEl.removeEventListener('pointerdown', onPointerDown, true);
    scrollEl.removeEventListener('pointermove', onPointerMove);
    scrollEl.removeEventListener('pointerup', onPointerUp);
    scrollEl.removeEventListener('pointercancel', onPointerUp);
    scrollEl.removeEventListener('error', onGridError);
    slider.removeEventListener('change', onSliderChange);
    compactMql.removeEventListener('change', onCompactChange);
    unsubSelection();
    bulkBar.destroy();
    personBtn.removeEventListener('click', onPersonBtnClick);
    jumpBtn.removeEventListener('click', onJumpClick);
    document.removeEventListener('click', onJumpDocClick);
    document.removeEventListener('keydown', onJumpKey);
    scrubber?.destroy();
    scrubber = null;
    grid?.destroy();
    grid = null;
    slots.clear();
    byId.clear();
  };
}
