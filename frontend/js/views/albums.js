// Vista Album: elenco degli album personali (stile Google Foto), Preferiti in testa.
// Stili in css/view-albums.css (caricato qui). Dati: GET/POST/PUT/DELETE /api/albums.
import { h, icon, debounce } from '../ui/el.js';
import { api, fileUrl, ApiError, onEvent } from '../api.js';
import { showSnackbar } from '../ui/snackbar.js';
import { confirmDialog, promptDialog } from '../ui/dialog.js';
import { emptyState, errorState } from '../ui/empty-state.js';
import { skeletonCard, skeletonGrid } from '../ui/skeleton.js';
import { createTile, updateTile } from '../ui/photo-tile.js';
import { createSelection, useRangeSelect } from '../ui/selection.js';
import { formatBytes, formatNumber, plural, missingNote } from '../ui/format.js';

const SEARCH_MIN_ALBUMS = 12; // sopra questa soglia compare la ricerca per nome
const SKELETON_COUNT = 6;
const NAME_MAX = 80;          // come NAME_MAX in fotoscan/server/routes_library.py
const CSS_URL = new URL('../../css/view-albums.css', import.meta.url).href;

function loadCss() {
  if ([...document.querySelectorAll('link[rel="stylesheet"]')].some((l) => l.href === CSS_URL)) return;
  document.head.append(h('link', { rel: 'stylesheet', href: CSS_URL }));
}

const countLabel = (n) => plural(n ?? 0, 'elemento', 'elementi');

/** Placeholder per album senza copertina (o copertina non caricabile). */
const placeholder = (name = 'photo_album') => h('div', { class: 'album-card__placeholder', 'aria-hidden': 'true' },
  icon(name, { filled: name === 'favorite' }));

/** Messaggio d'errore del server per il nome (409 duplicato, 400 non valido), altrimenti ''. */
function serverNameError(e) {
  if (!(e instanceof ApiError)) return '';
  if (e.status === 409) return 'Esiste già un album con questo nome.';
  if (e.status === 400) return e.message;
  return '';
}

export async function mount(root, { navigate, signal }) {
  loadCss();

  let alive = true;
  let status = 'loading';          // 'loading' | 'ready' | 'error'
  let albums = [];                 // elenco reale dal server
  let favCount;                    // undefined = in caricamento, null = non disponibile
  let sort = 'recent';             // 'recent' | 'name'
  let query = '';
  let current = null;              // album su cui e' aperto il menu
  let dialogOpen = false;          // un dialog di nome/conferma e' aperto: il FAB si nasconde
  let busy = false;                // creazione/rinomina in corso: niente doppi invii

  // --- Controlli di intestazione ---
  const search = h('md-outlined-text-field', {
    label: 'Cerca album', type: 'search', class: 'albums-search-field',
  }, icon('search', { slot: 'leading-icon' }));
  const searchWrap = h('div', { class: 'albums-search', hidden: true }, search);

  const sortSelect = h('md-outlined-select', { label: 'Ordina per', class: 'albums-sort' },
    h('md-select-option', { value: 'recent', selected: true }, h('div', { slot: 'headline' }, 'Più recenti')),
    h('md-select-option', { value: 'name' }, h('div', { slot: 'headline' }, 'Nome')));

  const section = h('section', { class: 'section albums-section' });
  // Annuncio per lo screen reader: aggiornato solo dalla ricerca (non l'intera griglia a ogni render).
  const srStatus = h('p', { class: 'albums-sr-status', role: 'status' });

  // --- Menu contestuale condiviso (un solo md-menu, riancorato alla card cliccata) ---
  const menu = document.createElement('md-menu');
  menu.className = 'albums-menu';
  menu.positioning = 'popover'; // top layer: non e' tagliato dallo scroll della view
  menu.append(
    h('md-menu-item', { onclick: () => current && rename(current) },
      icon('edit', { slot: 'start' }), h('div', { slot: 'headline' }, 'Rinomina')),
    h('md-menu-item', { class: 'albums-menu__danger', onclick: () => current && remove(current) },
      icon('delete', { slot: 'start' }), h('div', { slot: 'headline' }, 'Elimina album')));

  function openMenu(album, anchor) {
    current = album;
    menu.anchorElement = anchor;
    menu.open = true;
  }

  // --- Costruzione card ---
  function favoritesCard() {
    const count = favCount === undefined ? '…'
      : favCount === null ? ''
        : favCount === 0 ? 'Nessun preferito ancora'
          : countLabel(favCount);
    return h('div', { class: 'card card--clickable album-card album-card--fav' },
      h('button', {
        class: 'album-card__open', type: 'button',
        'aria-label': count ? `Preferiti, ${count}` : 'Preferiti',
        onclick: () => navigate('/gallery', { filter: 'favorites' }),
      },
        h('div', { class: 'album-card__cover' }, placeholder('favorite')),
        h('div', { class: 'album-card__body' },
          h('span', { class: 'album-card__name' }, 'Preferiti'),
          h('span', { class: 'album-card__meta' }, count))));
  }

  function albumCard(a) {
    const count = countLabel(a.count);
    const cover = a.cover_id != null
      ? h('img', {
        class: 'album-card__img', src: fileUrl.thumbStrict(a.cover_id, 320), alt: '',
        loading: 'lazy', decoding: 'async',
        onerror: (e) => e.currentTarget.replaceWith(placeholder()),
      })
      : placeholder();
    const menuBtn = h('md-icon-button', {
      class: 'album-card__menu', 'aria-label': `Opzioni per ${a.name}`,
      onclick: (e) => { e.stopPropagation(); openMenu(a, menuBtn); },
    }, icon('more_vert'));
    return h('div', { class: 'card card--clickable album-card', dataset: { id: a.id } },
      h('button', {
        class: 'album-card__open', type: 'button', 'aria-label': `${a.name}, ${count}`,
        onclick: () => navigate('/albums/' + a.id),
      },
        h('div', { class: 'album-card__cover' }, cover),
        h('div', { class: 'album-card__body' },
          h('span', { class: 'album-card__name' }, a.name),
          h('span', { class: 'album-card__meta' }, count))),
      menuBtn);
  }

  /** Senza album: stessa griglia, dopo Preferiti, una card di creazione (stessa dimensione delle card album). */
  function newAlbumCard() {
    return h('div', { class: 'card card--clickable album-card album-card--new' },
      h('button', {
        class: 'album-card__open', type: 'button', 'aria-label': 'Nuovo album', onclick: createAlbum,
      },
        h('div', { class: 'album-card__cover' }, icon('add')),
        h('div', { class: 'album-card__body' },
          h('span', { class: 'album-card__name' }, 'Nuovo album'),
          h('span', { class: 'album-card__meta', 'aria-hidden': 'true' }, ' '))));
  }

  /** Ordinamento e filtro per nome (la ricerca nasconde anche Preferiti). */
  function visibleAlbums() {
    const q = query.trim().toLowerCase();
    const list = albums.filter((a) => !q || a.name.toLowerCase().includes(q));
    if (sort === 'name') return list.sort((a, b) => a.name.localeCompare(b.name, 'it', { sensitivity: 'base' }));
    return list.sort((a, b) => (b.created || '').localeCompare(a.created || '') || b.id - a.id);
  }

  /** FAB solo quando l'elenco e' pronto e non vuoto: senza album c'e' gia' la card "Nuovo album". */
  function syncFab() {
    fab.hidden = dialogOpen || status !== 'ready' || albums.length === 0;
    fab.disabled = busy;
  }

  function render() {
    if (!alive) return;
    const body = [];
    if (status === 'loading') {
      section.setAttribute('aria-busy', 'true');
      body.push(h('div', { class: 'albums-grid' }, Array.from({ length: SKELETON_COUNT }, () => skeletonCard())));
    } else if (status === 'error') {
      section.removeAttribute('aria-busy');
      body.push(errorState({
        title: 'Impossibile caricare gli album',
        message: 'Controlla che l’app sia in esecuzione e riprova.',
        onRetry: load,
      }));
    } else {
      section.removeAttribute('aria-busy');
      const q = query.trim();
      const shown = visibleAlbums();
      const cards = q ? [] : [favoritesCard()];
      if (!albums.length && !q) cards.push(newAlbumCard());
      cards.push(...shown.map(albumCard));
      body.push(h('div', { class: 'albums-grid' }, cards));
      if (!albums.length && !q) {
        body.push(h('p', { class: 'albums-hint' }, 'Un album raccoglie le foto senza spostarle. Crea il primo album per iniziare.'));
      }
      if (albums.length && q && !shown.length) {
        body.push(emptyState({ icon: 'search_off', title: 'Nessun album trovato', message: `Nessun album contiene “${q}”.` }));
      }
    }
    section.replaceChildren(...body);
    sortSelect.hidden = albums.length < 2; // con un solo album l'ordine non serve
    const showSearch = albums.length > SEARCH_MIN_ALBUMS;
    searchWrap.hidden = !showSearch;
    if (!showSearch && query) { query = ''; search.value = ''; srStatus.textContent = ''; }
    syncFab();
  }

  // --- Azioni ---
  function fail(e) {
    if (!alive || e?.name === 'AbortError') return;
    let msg = e instanceof ApiError ? e.message : 'Operazione non riuscita. Riprova.';
    if (e instanceof ApiError && e.status === 409) msg = 'Esiste già un album con questo nome.';
    showSnackbar(msg, { error: true });
  }

  /** Controlli locali sul nome (lunghezza e duplicato, senza chiamare il server). */
  function nameProblem(name, exceptId) {
    if (name.length > NAME_MAX) return `Il nome può avere al massimo ${NAME_MAX} caratteri.`;
    const lower = name.toLowerCase();
    if (albums.some((x) => x.id !== exceptId && x.name.toLowerCase() === lower)) return 'Esiste già un album con questo nome.';
    return '';
  }

  /**
   * Dialog con campo nome. promptDialog chiude alla conferma, quindi con un errore il dialog
   * si riapre con il testo gia' scritto e il motivo nell'etichetta del campo.
   * @returns {Promise<string|null>} nome rifinito oppure null se annullato
   */
  async function askName({ title, confirmLabel, value, placeholder, error }) {
    dialogOpen = true;
    syncFab();
    try {
      const v = await promptDialog({
        title, label: error || 'Nome dell’album', value, placeholder, confirmLabel,
      });
      return v === null ? null : v.trim();
    } finally {
      dialogOpen = false;
      syncFab();
    }
  }

  function setBusy(on) {
    busy = on;
    syncFab();
    section.querySelectorAll('.album-card--new .album-card__open').forEach((b) => { b.disabled = on; });
  }

  async function createAlbum() {
    if (busy || !alive) return;
    let value = '';
    let error = '';
    for (; ;) {
      const name = await askName({
        title: 'Nuovo album', confirmLabel: 'Crea', value, placeholder: 'Es. Vacanze 2024', error,
      });
      if (name === null || !alive) return;
      if (!name) { value = ''; error = 'Inserisci un nome per l’album.'; continue; }
      const local = nameProblem(name, null);
      if (local) { value = name; error = local; continue; }
      setBusy(true);
      try {
        const album = await api.post('/albums', { name });
        navigate('/albums/' + album.id);
        return;
      } catch (e) {
        const msg = serverNameError(e);
        if (!msg) { fail(e); return; }
        value = name; error = msg;
      } finally {
        setBusy(false);
      }
    }
  }

  async function rename(a) {
    if (busy || !alive) return;
    let value = a.name;
    let error = '';
    for (; ;) {
      const name = await askName({ title: 'Rinomina album', confirmLabel: 'Salva', value, error });
      if (name === null || !alive) return;
      if (!name) { value = ''; error = 'Inserisci un nome per l’album.'; continue; }
      if (name === a.name) return;
      const local = nameProblem(name, a.id);
      if (local) { value = name; error = local; continue; }
      setBusy(true);
      try {
        const updated = await api.put(`/albums/${a.id}`, { name });
        albums = albums.map((x) => (x.id === a.id ? { ...x, ...updated } : x));
        render();
        showSnackbar('Album rinominato.');
        return;
      } catch (e) {
        const msg = serverNameError(e);
        if (!msg) { fail(e); return; }
        value = name; error = msg;
      } finally {
        setBusy(false);
      }
    }
  }

  const removing = new Set();   // album con eliminazione in corso (doppio clic)
  async function remove(a) {
    if (removing.has(a.id)) return;
    dialogOpen = true;
    syncFab();
    let ok;
    try {
      ok = await confirmDialog({
        title: `Eliminare l’album “${a.name}”?`,
        message: 'Le foto non vengono cancellate: restano dove sono. Viene eliminato solo l’album.',
        confirmLabel: 'Elimina',
        danger: true,
      });
    } finally {
      dialogOpen = false;
      syncFab();
    }
    if (!ok || !alive) return;
    removing.add(a.id);
    try {
      await api.del(`/albums/${a.id}`);
      albums = albums.filter((x) => x.id !== a.id);
      render();
      showSnackbar(`Album “${a.name}” eliminato.`);
    } catch (e) { fail(e); } finally { removing.delete(a.id); }
  }

  async function load() {
    status = 'loading';
    render();
    // Il conteggio dei Preferiti e' indipendente: un suo errore non blocca l'elenco.
    // Sono letture: un errore non apre snackbar (il conteggio mostra "" e la griglia lo spiega).
    const favP = api.get('/files', { favorite: 1, limit: 1 }, signal)
      .then((r) => { favCount = r.total; }, () => { favCount = null; });
    try {
      albums = await api.get('/albums', undefined, signal);
      status = 'ready';
    } catch (e) {
      if (!alive || e?.name === 'AbortError') return;
      status = 'error';
    }
    await favP;
    render();
  }

  // --- Montaggio ---
  const header = h('header', { class: 'view-header' },
    h('h1', { class: 't-headline' }, 'Album'),
    h('div', { class: 'view-header-actions' }, searchWrap, sortSelect));

  const fab = h('md-fab', { label: 'Nuovo album', class: 'albums-fab', onclick: createAlbum },
    icon('add', { slot: 'icon' }));

  const onSearch = debounce(() => {
    query = search.value;
    render();
    const q = query.trim();
    if (!q) { srStatus.textContent = ''; return; }
    const n = visibleAlbums().length;
    srStatus.textContent = n ? plural(n, 'album trovato', 'album trovati') : 'Nessun album trovato';
  }, 250);
  const onSort = () => { sort = sortSelect.value; render(); };
  search.addEventListener('input', onSearch);
  sortSelect.addEventListener('change', onSort);

  // --- Album suggeriti (temi ricorrenti scoperti + etichette da esempi + etichette di contesto) ---
  const SUGG_JOB = 'suggerimenti';
  const CTX_CACHE_KEY = 'fotoscan.ctxCache.v2'; // v2: conteggi con la formula del piano (i vecchi erano gonfiati)
  let suggestedItems = [];
  let visualItems = [];
  let contextItems = [];
  let suggJobId = null;
  let closePreviewSheet = null;
  const resolvingContexts = new Set(); // etichette in calcolo automatico
  const failedContexts = new Set();    // etichette il cui calcolo automatico non e' riuscito
  let resolvingAll = false;

  function readCtxCache() {
    try { return JSON.parse(localStorage.getItem(CTX_CACHE_KEY) || '{}') || {}; } catch { return {}; }
  }
  function saveCtxCache(name, prompt, total, sample) {
    try {
      const c = readCtxCache();
      c[name.toLowerCase()] = { prompt, count: total, sample: sample.slice(0, 4) };
      localStorage.setItem(CTX_CACHE_KEY, JSON.stringify(c));
    } catch { /* ignora */ }
  }

  const suggRunBtn = h('md-outlined-button', { onclick: () => runSuggestSearch() },
    icon('auto_awesome', { slot: 'icon' }), 'Cerca temi');
  const suggSettingsBtn = h('md-text-button', { onclick: () => navigate('/settings', { tab: 'suggested' }) },
    icon('tune', { slot: 'icon' }), 'Impostazioni etichette');
  const suggProgText = h('p', { class: 'md-typescale-body-small' }, 'Ricerca dei temi in corso…');
  const suggProg = h('div', { class: 'albums-suggested__progress', hidden: true, role: 'status' },
    h('md-linear-progress', { indeterminate: true }), suggProgText);
  const suggBody = h('div', {});
  const suggSection = h('section', { class: 'albums-suggested', 'aria-label': 'Album suggeriti' },
    h('div', { class: 'albums-suggested__head' },
      h('div', {},
        h('h2', { class: 'section-title' }, 'Album suggeriti'),
        h('p', { class: 'albums-suggested__sub' }, 'Temi ricorrenti riconosciuti nelle tue foto. Puoi regolarli o aggiungerne altri nelle impostazioni.')),
      h('div', { class: 'albums-suggested__actions' }, suggSettingsBtn, suggRunBtn)),
    suggProg,
    suggBody);

  function collageCover(ids) {
    const pics = (ids || []).slice(0, 4);
    if (!pics.length) return placeholder('auto_awesome');
    return h('div', { class: 'album-collage' },
      pics.map((id) => h('img', {
        src: fileUrl.thumbStrict(id, 256), alt: '', loading: 'lazy', decoding: 'async',
        onerror: (e) => { e.currentTarget.style.visibility = 'hidden'; },
      })));
  }

  function openSuggestedPreview({ title, albumName = '', endpoint, fallbackPrompt = '', onLoaded = null }) {
    if (closePreviewSheet) closePreviewSheet();
    const PAGE = 60;
    let done = false;
    let viewerOpen = false;
    let loadedItems = [];
    let totalCount = 0;
    let offset = 0;
    let loadingMore = false;
    let useFallback = false;
    let io = null;

    const saveAlbumBtn = h('md-filled-tonal-button', {
      hidden: true,
      onclick: async () => {
        if (!loadedItems.length) return;
        const targetName = (albumName || title).trim().slice(0, NAME_MAX);
        saveAlbumBtn.disabled = true;
        try {
          let target = albums.find((a) => a.name.toLowerCase() === targetName.toLowerCase());
          if (!target) target = await api.post('/albums', { name: targetName });
          await api.post(`/albums/${target.id}/items`, { file_ids: loadedItems.map((f) => f.id) });
          if (!alive) return;
          close();
          showSnackbar(`Album «${target.name}» aggiornato con ${plural(loadedItems.length, 'foto', 'foto')}.`);
          load();
        } catch (e) {
          saveAlbumBtn.disabled = false;
          if (alive) showSnackbar(e.message || 'Impossibile creare l’album.', { error: true });
        }
      },
    }, icon('photo_album', { slot: 'icon' }), 'Salva come album');

    const countNote = h('p', { class: 'muted md-typescale-body-medium' });
    const gridEl = h('div', { class: 'tile-grid albums-sheet__grid' });
    const moreBtn = h('md-outlined-button', { hidden: true, onclick: () => fetchNextPage() }, 'Carica altre foto');
    const sentinel = h('div', { style: 'height: 1px;' });
    const body = h('div', { class: 'albums-sheet__body' },
      h('md-linear-progress', { indeterminate: true }),
      skeletonGrid(18, { tile: 156 }));
    const sheetSel = createSelection();
    const sheetRange = useRangeSelect(sheetSel, () => loadedItems.map((f) => f.id));
    let sheetShift = false;
    let sheetCtrl = false;
    let sheetDrag = null;
    let sheetSuppressClick = false;
    gridEl.addEventListener('click', (e) => {
      if (sheetSuppressClick) { sheetSuppressClick = false; e.stopPropagation(); e.preventDefault(); return; }
      sheetShift = e.shiftKey;
      sheetCtrl = e.ctrlKey || e.metaKey;
    }, true);
    gridEl.addEventListener('keydown', (e) => { sheetShift = e.shiftKey; sheetCtrl = e.ctrlKey || e.metaKey; }, true);
    gridEl.addEventListener('pointerdown', (e) => {
      sheetDrag = null;
      if (e.pointerType === 'mouse' && e.button === 0 && (e.ctrlKey || e.metaKey)) {
        const tile = e.target.closest?.('.photo-tile');
        if (!tile) return;
        e.preventDefault();
        sheetDrag = { anchorId: Number(tile.dataset.id), baseIds: new Set(sheetSel.ids), moved: false };
      }
    }, true);
    gridEl.addEventListener('pointermove', (e) => {
      if (!sheetDrag) return;
      const tile = document.elementFromPoint(e.clientX, e.clientY)?.closest?.('.photo-tile');
      if (!tile) return;
      const id = Number(tile.dataset.id);
      if (id !== sheetDrag.anchorId) sheetDrag.moved = true;
      const ids = loadedItems.map((f) => f.id);
      const a = ids.indexOf(sheetDrag.anchorId), b = ids.indexOf(id);
      if (a < 0 || b < 0) return;
      const [lo, hi] = a <= b ? [a, b] : [b, a];
      const next = new Set(sheetDrag.baseIds);
      for (let i = lo; i <= hi; i++) next.add(ids[i]);
      sheetSel.selectAll([...next]);
    });
    const endSheetDrag = () => {
      if (sheetDrag?.moved) sheetSuppressClick = true;
      sheetDrag = null;
    };
    gridEl.addEventListener('pointerup', endSheetDrag);
    gridEl.addEventListener('pointercancel', endSheetDrag);

    const selCountEl = h('span', { class: 'md-typescale-title-small' });
    const trashBtn = h('md-filled-button', {
      class: 'albums-sheet__trash-btn',
      onclick: () => trashSelectedInSheet(),
    }, icon('delete', { slot: 'icon' }), 'Sposta nel Cestino');
    const selBarEl = h('div', { class: 'albums-sheet__selbar', hidden: true, role: 'region', 'aria-label': 'Azioni sulle foto selezionate' },
      selCountEl,
      h('div', { class: 'albums-sheet__selbar-actions' },
        h('md-text-button', { onclick: () => sheetSel.selectAll(loadedItems.map((f) => f.id)) }, 'Seleziona tutte'),
        h('md-text-button', { onclick: () => sheetSel.clear() }, 'Deseleziona'),
        trashBtn));

    function syncSheetTiles() {
      const on = sheetSel.count > 0;
      selBarEl.hidden = !on;
      if (on) selCountEl.textContent = plural(sheetSel.count, 'foto selezionata', 'foto selezionate');
      for (const tile of gridEl.querySelectorAll('.photo-tile')) {
        updateTile(tile, { selectable: true, selected: sheetSel.has(Number(tile.dataset.id)) });
      }
    }
    const unsubSheetSel = sheetSel.subscribe(syncSheetTiles);

    function makeSheetTile(f) {
      const tile = createTile(f, {
        size: 156,
        selectable: true,
        selected: sheetSel.has(f.id),
        onOpen: (file) => {
          if (sheetSel.count > 0 || sheetCtrl || sheetShift) {
            sheetRange(file.id, { shiftKey: sheetShift || (sheetCtrl && sheetSel.count > 0) });
            return;
          }
          const curIdx = loadedItems.findIndex((x) => x.id === file.id);
          if (curIdx >= 0) openViewerAt(curIdx);
        },
        onSelectToggle: (file) => sheetRange(file.id, { shiftKey: sheetShift }),
      });
      tile.dataset.id = String(f.id);
      return tile;
    }

    async function trashSelectedInSheet() {
      const ids = [...sheetSel.ids];
      if (!ids.length || trashBtn.disabled) return;
      const ok = await confirmDialog({
        title: `Spostare ${plural(ids.length, 'elemento', 'elementi')} nel Cestino?`,
        message: 'I file selezionati verranno spostati nel Cestino di Windows e rimossi dal catalogo. Potrai sempre recuperarli dal Cestino.',
        confirmLabel: 'Sposta nel Cestino',
        danger: true,
      });
      if (!ok || done) return;
      trashBtn.disabled = true;
      try {
        const res = await api.post('/cleanup/trash', { file_ids: ids, confirm: true });
        if (done) return;
        // Solo i file davvero spostati escono dall'anteprima: quelli in errore restano (e restano selezionati).
        const failedIds = new Set((res.errors || []).map((e) => e.id));
        const okIds = ids.filter((id) => !failedIds.has(id));
        const trashedSet = new Set(okIds);
        loadedItems = loadedItems.filter((f) => !trashedSet.has(f.id));
        totalCount = Math.max(loadedItems.length, totalCount - (res.trashed ?? okIds.length));
        for (const id of okIds) {
          gridEl.querySelector(`.photo-tile[data-id="${id}"]`)?.remove();
        }
        if (failedIds.size) {
          sheetSel.selectAll([...failedIds]);
          showSnackbar(`${plural(failedIds.size, 'file non spostato', 'file non spostati')}.`, { error: true });
        } else {
          sheetSel.clear();
        }
        updateCounter();
        const nOk = res.trashed ?? okIds.length;
        if (res.missing) showSnackbar(missingNote(res.missing));
        if (nOk > 0) showSnackbar(`${plural(nOk, 'elemento spostato', 'elementi spostati')} nel Cestino (${formatBytes(res.freed_bytes || 0)} liberati).`);
        onLoaded?.({ total: totalCount, items: loadedItems });
      } catch (e) {
        if (!done) showSnackbar(e.message || 'Impossibile spostare nel Cestino.', { error: true });
      } finally {
        trashBtn.disabled = false;
      }
    }

    const closeBtn = h('md-icon-button', { 'aria-label': 'Chiudi', onclick: () => close() }, icon('close'));
    const scrim = h('div', { class: 'albums-sheet-scrim', onclick: () => close() });
    const sheet = h('div', { class: 'albums-sheet', role: 'dialog', 'aria-modal': 'true', 'aria-label': title },
      h('div', { class: 'albums-sheet__head' },
        h('h2', { class: 'md-typescale-title-large' }, title), saveAlbumBtn, closeBtn),
      selBarEl,
      body);
    const onKey = (e) => {
      if (e.key === 'Escape' && !viewerOpen) {
        e.preventDefault();
        if (sheetSel.count > 0) sheetSel.clear();
        else close();
      } else if (e.key === 'Delete' && !viewerOpen && sheetSel.count > 0) {
        e.preventDefault();
        trashSelectedInSheet();
      }
    };
    function close() {
      if (done) return;
      done = true;
      unsubSheetSel();
      if (io) { io.disconnect(); io = null; }
      document.removeEventListener('keydown', onKey, true);
      scrim.remove(); sheet.remove();
      if (closePreviewSheet === close) closePreviewSheet = null;
    }
    closePreviewSheet = close;
    document.addEventListener('keydown', onKey, true);
    document.body.append(scrim, sheet);
    closeBtn.focus();

    async function openViewerAt(index) {
      viewerOpen = true;
      try {
        const { openViewer } = await import('./viewer.js');
        openViewer({
          items: loadedItems,
          index,
          onClose: () => { viewerOpen = false; },
          onDelete: (f) => {
            loadedItems = loadedItems.filter((x) => x.id !== f.id);
            totalCount = Math.max(loadedItems.length, totalCount - 1);
            gridEl.querySelector(`.photo-tile[data-id="${f.id}"]`)?.remove();
            updateCounter();
          },
        });
      } catch {
        viewerOpen = false;
        showSnackbar('Visualizzatore non disponibile.', { error: true });
      }
    }

    const hintNote = h('span', { class: 'muted md-typescale-body-small' }, 'Ctrl+clic per selezionare · Shift+clic per intervallo');
    const metaRow = h('div', { class: 'albums-sheet__meta' }, countNote, hintNote);

    function updateCounter(live = false) {
      const liveNote = live ? ' (calcolate dal modello)' : '';
      countNote.textContent = loadedItems.length < totalCount
        ? `Mostrate ${formatNumber(loadedItems.length)} di ${formatNumber(totalCount)} foto${liveNote}.`
        : `${plural(totalCount, 'foto riconosciuta', 'foto riconosciute')}${liveNote}.`;
      moreBtn.hidden = useFallback || loadedItems.length >= totalCount;
    }

    async function fetchNextPage() {
      if (done || !alive || loadingMore) return;
      if (offset > 0 && (useFallback || loadedItems.length >= totalCount)) return;
      loadingMore = true;
      moreBtn.disabled = true;
      for (let attempt = 1; !done; attempt++) {
        try {
          let data;
          if (useFallback && fallbackPrompt) {
            const r = await api.post('/labels/test', { prompt: fallbackPrompt });
            data = { items: r?.items || [], total: r?.coverage?.above_threshold ?? (r?.items?.length || 0), live: true };
          } else {
            data = await api.get(endpoint, { limit: PAGE, offset });
          }
          if (done || !alive) return;
          const batch = data?.items || [];
          const isFirst = offset === 0;
          totalCount = Math.max(data?.total || 0, loadedItems.length + batch.length);
          loadedItems.push(...batch);
          offset += batch.length;

          if (isFirst) {
            saveAlbumBtn.hidden = loadedItems.length === 0;
            onLoaded?.({ total: totalCount, items: loadedItems });
            if (!loadedItems.length) {
              body.replaceChildren(emptyState({ icon: 'image_search', title: 'Nessuna foto trovata' }));
              loadingMore = false;
              return;
            }
            gridEl.replaceChildren(...batch.map((f) => makeSheetTile(f)));
            body.replaceChildren(metaRow, gridEl, h('div', { style: 'display:flex;justify-content:center;padding:8px 0;' }, moreBtn), sentinel);
            if (!useFallback && loadedItems.length < totalCount) {
              io = new IntersectionObserver((entries) => {
                if (entries.some((e) => e.isIntersecting)) fetchNextPage();
              }, { root: body, rootMargin: '300px 0px' });
              io.observe(sentinel);
            }
          } else if (batch.length) {
            const frag = document.createDocumentFragment();
            batch.forEach((f) => frag.append(makeSheetTile(f)));
            gridEl.append(frag);
          } else {
            totalCount = loadedItems.length;
          }
          updateCounter(!!data?.live);
          loadingMore = false;
          moreBtn.disabled = false;
          return;
        } catch (e) {
          if (done || !alive) return;
          if (!useFallback && fallbackPrompt && e instanceof ApiError && e.status === 404) {
            useFallback = true;
            continue;
          }
          if (e instanceof ApiError && e.status === 503 && attempt < 90) {   // il modello grande puo' impiegare un paio di minuti a caricarsi
            if (offset === 0) {
              body.replaceChildren(
                h('p', { class: 'muted md-typescale-body-medium' }, 'Sto preparando il modello di ricerca… attendi qualche secondo.'),
                h('md-linear-progress', { indeterminate: true }),
                skeletonGrid(12, { tile: 104 }));
            }
            await new Promise((r) => setTimeout(r, 2000));
            continue;
          }
          loadingMore = false;
          moreBtn.disabled = false;
          if (offset === 0) {
            body.replaceChildren(emptyState({ icon: 'error_outline', title: 'Anteprima non disponibile', message: e.message }));
          } else {
            showSnackbar(e.message || 'Impossibile caricare altre foto.', { error: true });
          }
          return;
        }
      }
    }
    fetchNextPage();
  }

  function suggestedCard(it) {
    const monthsPart = it.months ? ` · ${plural(it.months, 'mese', 'mesi')}` : '';
    const acceptBtn = h('md-filled-tonal-button', {
      onclick: async () => {
        acceptBtn.disabled = true;
        try {
          await api.post(`/labels/suggestions/${it.id}/accept`, { name: it.name });
          if (!alive) return;
          showSnackbar(`Album suggerito «${it.name}» salvato nei tuoi temi.`);
          loadSuggested();
        } catch (e) {
          acceptBtn.disabled = false;
          if (alive) showSnackbar(e.message || 'Impossibile salvare il tema.', { error: true });
        }
      },
    }, 'Salva');
    const ignoreBtn = h('md-text-button', {
      onclick: async () => {
        ignoreBtn.disabled = true;
        try {
          await api.post(`/labels/suggestions/${it.id}/dismiss`, {});
          if (alive) loadSuggested();
        } catch (e) {
          ignoreBtn.disabled = false;
          if (alive) showSnackbar(e.message || 'Operazione non riuscita.', { error: true });
        }
      },
    }, 'Ignora');
    return h('div', { class: 'card card--clickable album-card' },
      h('span', { class: 'album-card__badge album-card__badge--new' }, 'Proposto'),
      h('button', {
        class: 'album-card__open', type: 'button',
        'aria-label': `Anteprima tema suggerito ${it.name}`,
        onclick: () => openSuggestedPreview({ title: `Tema suggerito: ${it.name}`, endpoint: `/labels/suggestions/${it.id}/preview` }),
      },
        h('div', { class: 'album-card__cover' }, collageCover(it.sample)),
        h('div', { class: 'album-card__body' },
          h('span', { class: 'album-card__name' }, it.name),
          h('span', { class: 'album-card__meta' }, `${formatNumber(it.count)} foto${monthsPart}`))),
      h('div', { class: 'album-card__footer' }, ignoreBtn, acceptBtn));
  }

  function visualAlbumCard(v) {
    return h('div', { class: 'card card--clickable album-card' },
      h('span', { class: 'album-card__badge' }, 'Automatico'),
      h('button', {
        class: 'album-card__open', type: 'button',
        'aria-label': `Apri album automatico ${v.name}`,
        onclick: () => openSuggestedPreview({ title: v.name, albumName: v.name, endpoint: `/labels/visual/${encodeURIComponent(v.name)}/preview` }),
      },
        h('div', { class: 'album-card__cover' }, collageCover(v.sample)),
        h('div', { class: 'album-card__body' },
          h('span', { class: 'album-card__name' }, v.name),
          h('span', { class: 'album-card__meta' }, plural(v.count, 'foto di esempio', 'foto di esempio')))));
  }

  function contextAlbumCard(c) {
    const meta = c.in_plan
      ? plural(c.count, 'foto', 'foto')
      : resolvingContexts.has(c.name) ? 'Calcolo delle foto in corso…'
        : failedContexts.has(c.name) ? 'Calcolo non riuscito · tocca per riprovare'
          : 'Nuova etichetta · tocca per cercare';
    return h('div', { class: 'card card--clickable album-card' },
      h('span', { class: `album-card__badge${c.in_plan ? '' : ' album-card__badge--new'}` }, c.in_plan ? 'Etichetta' : 'Nuova'),
      h('button', {
        class: 'album-card__open', type: 'button',
        'aria-label': `Apri album etichetta ${c.name}`,
        onclick: () => openSuggestedPreview({
          title: c.name,
          albumName: c.name,
          endpoint: `/labels/contexts/${encodeURIComponent(c.name)}/preview`,
          fallbackPrompt: c.prompt || c.name,
          onLoaded: ({ total, items }) => {
            const sample = items.slice(0, 4).map((f) => f.id);
            saveCtxCache(c.name, c.prompt || '', total, sample);
            failedContexts.delete(c.name);
            contextItems = contextItems.map((x) => (x.name === c.name
              ? { ...x, in_plan: true, count: total, sample: sample.length ? sample : x.sample }
              : x));
            renderSuggested();
          },
        }),
      },
        h('div', { class: 'album-card__cover' }, collageCover(c.sample)),
        h('div', { class: 'album-card__body' },
          h('span', { class: 'album-card__name' }, c.name),
          h('span', { class: 'album-card__meta' }, meta))));
  }

  function renderSuggested() {
    if (!alive) return;
    const sortedContexts = [...contextItems].sort((a, b) => Number(a.in_plan) - Number(b.in_plan) || b.count - a.count);
    const cards = [
      ...suggestedItems.map(suggestedCard),
      ...visualItems.map(visualAlbumCard),
      ...sortedContexts.map(contextAlbumCard),
    ];
    if (cards.length) {
      suggBody.replaceChildren(h('div', { class: 'albums-grid fs-stagger' }, cards));
    } else if (!suggJobId) {
      suggBody.replaceChildren(
        h('div', { class: 'card albums-suggested__cta' },
          h('div', { class: 'albums-suggested__cta-copy' },
            h('strong', {}, 'Scopri gli album suggeriti dal tuo archivio'),
            h('p', {}, 'PhotoScan analizza le foto simili che tornano nel tempo (palestra, viaggi, cucina…) e ti propone album automatici pronti all’uso.')),
          h('md-filled-tonal-button', { onclick: () => runSuggestSearch() },
            icon('auto_awesome', { slot: 'icon' }), 'Cerca album suggeriti')));
    } else {
      suggBody.replaceChildren();
    }
  }

  async function runSuggestSearch() {
    if (suggJobId) return;
    suggRunBtn.disabled = true;
    try {
      const r = await api.post('/labels/suggestions/run', {});
      if (!alive) return;
      suggJobId = r.job_id;
      suggProg.hidden = false;
      suggProgText.textContent = 'Ricerca dei temi ricorrenti in corso…';
      renderSuggested();
    } catch (e) {
      if (!alive) return;
      if (e.status === 409) { loadSuggested(); return; }
      suggRunBtn.disabled = false;
      showSnackbar(e.message || 'Ricerca non avviata.', { error: true });
    }
  }

  async function loadSuggested() {
    const [sRes, vRes, cRes] = await Promise.allSettled([
      api.get('/labels/suggestions', undefined, signal),
      api.get('/labels/visual', undefined, signal),
      api.get('/labels/contexts', undefined, signal),
    ]);
    if (!alive) return;
    if (sRes.status === 'fulfilled') {
      const s = sRes.value;
      suggestedItems = s?.items || [];
      if (s?.job_id) {
        suggJobId = s.job_id;
        suggRunBtn.disabled = true;
        suggProg.hidden = false;
      }
    }
    if (vRes.status === 'fulfilled') {
      visualItems = Array.isArray(vRes.value) ? vRes.value : [];
    }
    const localCache = readCtxCache();
    const hydrate = (list) => list.map((it) => {
      if (it.in_plan) return it;
      const hit = localCache[it.name.toLowerCase()];
      if (hit && hit.prompt === (it.prompt || '')) {
        return { ...it, in_plan: true, count: hit.count || 0, sample: hit.sample || [] };
      }
      return it;
    });
    if (cRes.status === 'fulfilled' && Array.isArray(cRes.value)) {
      contextItems = hydrate(cRes.value);
    } else {
      // Fallback se il server non è ancora stato riavviato: legge le etichette da /settings.
      try {
        const cfg = await api.get('/settings', undefined, signal);
        if (!alive) return;
        contextItems = hydrate((cfg?.labels || []).map((l) => ({
          name: l.name, prompt: l.prompt || '', count: 0, in_plan: false, sample: [],
        })));
      } catch { /* facoltativo */ }
    }
    renderSuggested();
    resolveContexts();
  }

  /** Calcola da sole (una alla volta) le etichette non ancora pronte: niente piu' «tocca per cercare». */
  async function resolveContexts() {
    if (resolvingAll) return;
    resolvingAll = true;
    try {
      for (const c of contextItems.filter((x) => !x.in_plan && !failedContexts.has(x.name))) {
        if (!alive) return;
        resolvingContexts.add(c.name);
        renderSuggested();
        let ok = false;
        for (let attempt = 1; alive && attempt <= 90 && !ok; attempt++) {
          try {
            const data = await api.get(`/labels/contexts/${encodeURIComponent(c.name)}/preview`, { limit: 4, offset: 0 });
            if (!alive) return;
            const sample = (data?.items || []).slice(0, 4).map((f) => f.id);
            const total = data?.total || 0;
            saveCtxCache(c.name, c.prompt || '', total, sample);
            contextItems = contextItems.map((x) => (x.name === c.name ? { ...x, in_plan: true, count: total, sample } : x));
            ok = true;
          } catch (e) {
            if (e instanceof ApiError && e.status === 503) { await new Promise((r) => setTimeout(r, 2000)); continue; } // modello in caricamento
            break; // errore vero: l'etichetta resta da riprovare a mano
          }
        }
        resolvingContexts.delete(c.name);
        if (!ok && alive) failedContexts.add(c.name);
        renderSuggested();
      }
    } finally {
      resolvingAll = false;
    }
  }

  const offSuggJob = onEvent('job', (job) => {
    if (!alive || job.name !== SUGG_JOB) return;
    if (job.state === 'running') {
      suggJobId = job.id;
      suggRunBtn.disabled = true;
      suggProg.hidden = false;
      suggProgText.textContent = job.message || 'Ricerca dei temi in corso…';
      return;
    }
    if (!suggJobId || job.id !== suggJobId) return;
    suggJobId = null;
    suggRunBtn.disabled = false;
    suggProg.hidden = true;
    if (job.state === 'done') {
      showSnackbar('Ricerca album suggeriti completata.');
      loadSuggested();
    } else {
      showSnackbar(job.error || 'Ricerca non riuscita.', { error: true });
      renderSuggested();
    }
  });

  // Wrapper proprio: la classe albums-view non deve restare sull'outlet condiviso.
  const wrap = h('div', { class: 'view albums-view' }, header, srStatus, section, suggSection);
  root.append(wrap, menu, fab);

  render();
  load();
  loadSuggested();

  return () => {
    alive = false;
    search.removeEventListener('input', onSearch);
    sortSelect.removeEventListener('change', onSort);
    offSuggJob();
    if (closePreviewSheet) closePreviewSheet();
    menu.remove();
    fab.remove();
  };
}
