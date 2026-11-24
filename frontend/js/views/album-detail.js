// Dettaglio di un album: copertina, nome modificabile, griglia virtualizzata con selezione multipla,
// selettore a schermo intero per aggiungere foto. Dati: /api/albums, /api/albums/{id}/files, /api/files.
// Gli album sono solo collegamenti nel catalogo: eliminare o svuotare un album non tocca mai i file.
import { h, icon } from '../ui/el.js';
import { api, ApiError, fileUrl } from '../api.js';
import { VirtualGrid } from '../ui/virtual-grid.js';
import { createTile, updateTile } from '../ui/photo-tile.js';
import { createSelection, selectionBar } from '../ui/selection.js';
import { filterChips } from '../ui/chips.js';
import { emptyState, errorState } from '../ui/empty-state.js';
import { skeletonGrid } from '../ui/skeleton.js';
import { showSnackbar } from '../ui/snackbar.js';
import { confirmDialog, promptDialog } from '../ui/dialog.js';
import { formatDateShort, formatNumber, plural, mesiNomi } from '../ui/format.js';

const PAGE = 120;
const cap = (s) => s.charAt(0).toUpperCase() + s.slice(1);
const tileSize = () => (window.matchMedia?.('(max-width: 599px)').matches ? 110 : 180);

let cssLoaded = false;
function loadCss() {
  if (cssLoaded || document.querySelector('link[data-view="album-detail"]')) { cssLoaded = true; return; }
  cssLoaded = true;
  const l = document.createElement('link');
  l.rel = 'stylesheet'; l.href = 'css/view-album-detail.css'; l.dataset.view = 'album-detail';
  document.head.append(l);
}

/** "9 giu 2020 – 3 set 2021", oppure una sola data se coincidono. */
function rangeText(first, last) {
  if (!first?.date) return '';
  const a = formatDateShort(first.date);
  const b = last?.date ? formatDateShort(last.date) : a;
  return a === b ? a : `${a} – ${b}`;
}

/** Tile attualmente montate dentro un contenitore (il DOM e' la sola fonte: niente mappe che trattengono nodi staccati). */
const mountedTiles = (container) => [...container.querySelectorAll('.photo-tile[data-id]')];
const tileFor = (container, id) => container.querySelector(`.photo-tile[data-id="${id}"]`);

export async function mount(root, ctx = {}) {
  loadCss();
  const albumId = Number(ctx.params?.id);
  const path = `/albums/${albumId}`;
  const ac = new AbortController();
  const signal = ac.signal;
  const onCtxAbort = () => ac.abort();
  ctx.signal?.addEventListener('abort', onCtxAbort);
  let alive = true;
  let gen = 0;

  const state = { album: null, range: '', loaded: false };
  const selection = createSelection();
  let vg = null;
  let bar = null;
  let unsubSel = null;
  let picker = null;
  let scroller = null;
  let mq = null;
  const onMq = () => vg?.setTileSize(tileSize());

  const fail = (e) => {
    if (!alive || e?.name === 'AbortError') return;
    showSnackbar(e?.message || 'Operazione non riuscita', { error: true });
  };
  const onGridError = (e) => fail(e.detail?.error);

  // ---------- Intestazione ----------
  const coverImg = h('img', { class: 'album-hero__img', alt: '', decoding: 'async', hidden: true });
  const placeholder = icon('photo_album');
  placeholder.classList.add('album-hero__placeholder');
  const mediaEl = h('div', { class: 'album-hero__media skeleton', 'aria-hidden': 'true' }, coverImg, placeholder);
  const nameEl = h('h1', { class: 'album-hero__name md-typescale-headline-medium' });
  const metaEl = h('p', { class: 'album-hero__meta md-typescale-body-large' });
  const editBtn = h('md-icon-button', {
    class: 'album-hero__edit', 'aria-label': 'Rinomina album', title: 'Rinomina album',
    onclick: () => renameAlbum(),
  }, icon('edit'));
  const hero = h('section', { class: 'album-hero is-loading', 'aria-label': 'Intestazione album' },
    mediaEl,
    h('div', { class: 'album-hero__body' }, h('div', { class: 'album-hero__title' }, nameEl, editBtn), metaEl));

  coverImg.addEventListener('load', () => {
    mediaEl.classList.remove('skeleton');
    hero.classList.add('has-cover');
  });
  coverImg.addEventListener('error', () => {
    coverImg.hidden = true;
    mediaEl.classList.remove('skeleton');
    hero.classList.remove('has-cover');
  });

  const addBtn = h('md-filled-button', { class: 'album-detail__add', onclick: () => openPicker() },
    icon('add_photo_alternate', { slot: 'icon' }), 'Aggiungi foto');
  const deleteBtn = h('md-outlined-button', { class: 'album-detail__danger', onclick: () => deleteAlbum() },
    icon('delete', { slot: 'icon' }), 'Elimina album');
  const actions = h('div', { class: 'view-header album-detail__actions' },
    h('div', { class: 'view-header-actions' }, addBtn, deleteBtn));

  // ---------- Griglia e stati ----------
  const gridHost = h('div', { class: 'album-detail__grid' }, skeletonGrid(24, { tile: tileSize() }));
  const emptyHost = h('div', { class: 'album-detail__empty', hidden: true });
  const view = h('div', { class: 'view album-detail', 'aria-busy': 'true' }, hero, actions, gridHost, emptyHost);
  root.replaceChildren(view);

  function renderHero() {
    const a = state.album;
    nameEl.textContent = a.name;
    metaEl.textContent = [plural(a.count, 'elemento', 'elementi'), state.range].filter(Boolean).join(' · ');
    hero.classList.remove('is-loading');
    if (a.cover_id) {
      // thumbStrict: 404 se il file non ha un'immagine vera, cosi' scatta onerror e resta il segnaposto a tema.
      const src = fileUrl.thumbStrict(a.cover_id, 512);
      if (coverImg.dataset.src !== src) {
        coverImg.dataset.src = src;
        coverImg.hidden = false;
        mediaEl.classList.add('skeleton');
        coverImg.src = src;
      }
    } else {
      coverImg.hidden = true;
      coverImg.removeAttribute('src');
      delete coverImg.dataset.src;
      mediaEl.classList.remove('skeleton');
      hero.classList.remove('has-cover');
    }
  }

  function showNotFound() {
    disposeGrid();
    view.replaceChildren(emptyState({
      icon: 'photo_album',
      title: 'Album non trovato',
      message: 'Potrebbe essere stato eliminato, oppure il link non è corretto.',
      actionLabel: 'Torna agli album',
      onAction: () => ctx.navigate?.('/albums'),
    }));
    view.removeAttribute('aria-busy');
  }

  // ---------- Caricamento ----------
  /** Ultimo elemento CON data (gli elementi senza data stanno in fondo: ricerca binaria, O(log n) richieste). */
  async function lastDatedItem(count, first, last) {
    if (last?.date) return last;
    let lo = 0;          // indice noto con data (il primo)
    let hi = count - 1;  // indice noto senza data
    let good = first;
    while (hi - lo > 1) {
      const mid = Math.floor((lo + hi) / 2);
      const it = (await api.get(`${path}/files`, { offset: mid, limit: 1 }, signal)).items[0];
      if (!alive) return good;
      if (it?.date) { lo = mid; good = it; } else hi = mid;
    }
    return good;
  }

  /** Ricalcola intervallo di date, intestazione e griglia a partire da state.album (count gia' aggiornato). */
  async function refreshMeta(my) {
    const count = state.album.count;
    let range = '';
    if (count > 0) {
      // La lista e' ordinata per data crescente: primo e ultimo elemento danno l'intervallo.
      const [first, last] = await Promise.all([
        api.get(`${path}/files`, { offset: 0, limit: 1 }, signal),
        api.get(`${path}/files`, { offset: count - 1, limit: 1 }, signal),
      ]);
      if (!alive || my !== gen) return;
      const firstItem = first.items[0];
      const lastItem = await lastDatedItem(count, firstItem, last.items[0]);
      if (!alive || my !== gen) return;
      range = rangeText(firstItem, lastItem);
    }
    if (!alive || my !== gen) return;
    state.range = range;
    state.loaded = true;
    view.removeAttribute('aria-busy');
    hero.classList.remove('is-loading');
    addBtn.disabled = false;
    deleteBtn.disabled = false;
    renderHero();
    syncGrid(count);
  }

  async function loadAlbum() {
    const my = ++gen;
    try {
      if (!Number.isInteger(albumId)) return showNotFound();
      const list = await api.get('/albums', undefined, signal);
      if (!alive || my !== gen) return;
      const album = list.find((a) => a.id === albumId);
      if (!album) return showNotFound();
      state.album = album;
      await refreshMeta(my);
    } catch (e) {
      if (!alive || e?.name === 'AbortError') return;
      if (e instanceof ApiError && e.status === 404) return showNotFound();
      if (!state.loaded) {
        // Primo caricamento fallito: niente album noto, quindi intestazione neutra e azioni disattivate.
        view.removeAttribute('aria-busy');
        hero.classList.remove('is-loading');
        nameEl.textContent = 'Album';
        metaEl.textContent = 'Dati non disponibili';
        addBtn.disabled = true;
        deleteBtn.disabled = true;
        gridHost.hidden = false;
        emptyHost.hidden = true;
        gridHost.replaceChildren(errorState({ message: e.message, onRetry: () => loadAlbum() }));
      }
      fail(e);
    }
  }

  function syncGrid(total) {
    if (total === 0) {
      disposeGridParts();
      gridHost.hidden = true;
      emptyHost.hidden = false;
      emptyHost.replaceChildren(emptyState({
        icon: 'add_photo_alternate',
        title: 'Album vuoto',
        message: 'Aggiungi le foto che vuoi raccogliere in questo album.',
        actionLabel: 'Aggiungi foto',
        onAction: () => openPicker(),
      }));
      return;
    }
    emptyHost.hidden = true;
    emptyHost.replaceChildren();
    gridHost.hidden = false;
    if (!vg) createGrid();
    else vg.reload();
  }

  // ---------- Griglia principale ----------
  function createGrid() {
    gridHost.replaceChildren();
    // Lo scroll avviene nel contenitore della view (.view-outlet); la griglia usa i rettangoli reali, quindi il layer puo' stare in gridHost.
    scroller = root.closest('.view-outlet') || root;
    vg = new VirtualGrid(scroller, {
      tile: tileSize(),
      gap: 4,
      pageSize: PAGE,
      fetchPage: (offset, limit, sig) => api.get(`${path}/files`, { offset, limit }, sig ?? signal),
      renderItem: (file, index) => makeTile(file, index),
    });
    gridHost.append(vg.layer);
    scroller.addEventListener('error', onGridError);
    vg.reload();

    bar = selectionBar(gridHost, {
      selection,
      actions: [
        {
          id: 'remove', icon: 'playlist_remove', text: 'Rimuovi',
          label: "Rimuovi dall'album (la foto resta nell'archivio)",
          run: (ids) => removeFromAlbum(ids),
        },
        {
          id: 'fav', icon: 'favorite', text: 'Preferiti',
          label: 'Aggiungi o togli dai preferiti',
          run: (ids) => setFavorite(ids),
        },
        {
          id: 'cover', icon: 'wallpaper', text: 'Copertina',
          label: "Usa la foto selezionata come copertina dell'album",
          run: (ids) => (ids.length === 1
            ? setCover(ids[0])
            : showSnackbar('Per la copertina seleziona una sola foto', { error: true })),
        },
      ],
    });
    unsubSel = selection.subscribe(() => {
      for (const t of mountedTiles(gridHost)) updateTile(t, { selected: selection.has(Number(t.dataset.id)) });
    });

    if (!mq && window.matchMedia) {
      mq = window.matchMedia('(max-width: 599px)');
      mq.addEventListener('change', onMq);
    }
  }

  function makeTile(file, index) {
    const tile = createTile(file, {
      size: tileSize(),
      selectable: true,
      selected: selection.has(file.id),
      onOpen: () => (selection.count ? selection.toggle(file.id) : openViewerAt(index)),
      onSelectToggle: (f, on) => (on ? selection.add(f.id) : selection.remove(f.id)),
    });
    tile.dataset.id = file.id;
    return tile;
  }

  async function openViewerAt(index) {
    const all = vg?.items || [];
    if (!all[index]) return;
    // Viewer su un blocco contiguo di elementi gia' caricati che contiene la foto aperta.
    let lo = index;
    while (lo > 0 && all[lo - 1]) lo -= 1;
    let hi = index;
    while (hi + 1 < all.length && all[hi + 1]) hi += 1;
    const items = all.slice(lo, hi + 1);
    let nextOffset = hi + 1;
    try {
      const { openViewer } = await import('./viewer.js');
      openViewer({
        items,
        index: index - lo,
        loadMore: async () => {
          const page = await api.get(`${path}/files`, { offset: nextOffset, limit: PAGE }, signal);
          nextOffset += page.items.length;
          return page.items;
        },
        onChange: (file) => {
          const t = tileFor(gridHost, file.id);
          if (t) updateTile(t, { favorite: !!file.favorite });
        },
      });
    } catch (e) {
      fail(e);
    }
  }

  // ---------- Azioni sulla selezione ----------
  async function removeFromAlbum(ids) {
    const prevCover = state.album?.cover_id ?? null;
    const coverGone = prevCover != null && ids.includes(prevCover);
    try {
      const res = await api.del(`${path}/items`, undefined, { file_ids: ids });
      if (!alive) return;
      selection.clear();
      if (!res.removed) {
        await loadAlbum();
        return showSnackbar("Le foto selezionate non erano nell'album");
      }
      // Aggiornamento locale del conteggio: il server azzera la copertina se e' stata tolta.
      state.album = { ...state.album, count: Math.max(0, state.album.count - res.removed), cover_id: coverGone ? null : state.album.cover_id };
      await refreshMeta(++gen);
      showSnackbar(`${plural(res.removed, 'foto rimossa', 'foto rimosse')} dall'album`, {
        actionLabel: 'Annulla',
        duration: 6000,
        onAction: () => undoRemove(ids, coverGone ? prevCover : null),
      });
    } catch (e) {
      fail(e);
    }
  }

  async function undoRemove(ids, coverId) {
    try {
      const res = await api.post(`${path}/items`, { file_ids: ids });
      if (!alive) return;
      // Il server ha azzerato la copertina alla rimozione: la si rimette se era quella tolta.
      if (coverId != null) await api.put(path, { cover_file_id: coverId }).catch(() => null);
      if (!alive) return;
      await loadAlbum();
      showSnackbar(plural(res.added, 'foto ripristinata', 'foto ripristinate'));
    } catch (e) {
      fail(e);
    }
  }

  async function setFavorite(ids) {
    const loaded = (vg?.getItems() || []).filter((f) => ids.includes(f.id));
    // Se qualche foto selezionata non e' ancora in memoria, il valore sicuro e' "aggiungi": non si conosce lo stato.
    const allKnown = loaded.length === ids.length;
    const value = !(allKnown && loaded.every((f) => f.favorite));
    try {
      await api.post('/files/favorite', { ids, value });
      if (!alive) return;
      for (const f of loaded) {
        f.favorite = value;
        const t = tileFor(gridHost, f.id);
        if (t) updateTile(t, { favorite: value });
      }
      showSnackbar(value ? 'Aggiunte ai preferiti' : 'Rimosse dai preferiti');
    } catch (e) {
      fail(e);
    }
  }

  async function setCover(fileId) {
    try {
      const updated = await api.put(path, { cover_file_id: fileId });
      if (!alive) return;
      state.album = { ...state.album, ...updated };
      selection.clear();
      renderHero();
      showSnackbar('Copertina aggiornata');
    } catch (e) {
      fail(e);
    }
  }

  // ---------- Intestazione: rinomina ed eliminazione ----------
  /** promptDialog si chiude alla conferma: se il server rifiuta il nome (409), il dialogo si riapre con il testo gia' scritto. */
  async function renameAlbum(prefill, errorText) {
    if (!state.album) return;
    const next = await promptDialog({
      title: 'Rinomina album',
      label: errorText || "Nome dell'album",
      value: prefill ?? state.album.name,
      confirmLabel: 'Salva',
      placeholder: 'Es. Vacanze in Sardegna',
    });
    if (next === null || !alive) return;
    const name = next.trim();
    if (!name || name === state.album.name) return;
    try {
      const updated = await api.put(path, { name });
      if (!alive) return;
      state.album = { ...state.album, ...updated };
      renderHero();
      showSnackbar('Nome aggiornato');
    } catch (e) {
      if (!alive || e?.name === 'AbortError') return;
      if (e instanceof ApiError && e.status === 409) return renameAlbum(name, e.message);
      fail(e);
    }
  }

  async function deleteAlbum() {
    const name = state.album?.name ?? '';
    const ok = await confirmDialog({
      title: `Eliminare l'album "${name}"?`,
      message: "Le foto non vengono cancellate: restano nell'archivio. Viene eliminato solo l'album.",
      confirmLabel: 'Elimina',
      danger: true,
    });
    if (!ok || !alive) return;
    try {
      await api.del(path);
      if (!alive) return;
      showSnackbar('Album eliminato');
      ctx.navigate?.('/albums');
    } catch (e) {
      fail(e);
    }
  }

  // ---------- Selettore "Aggiungi foto" a schermo intero ----------
  function openPicker() {
    if (picker) return;
    const psel = createSelection();
    const pac = new AbortController();
    const previouslyFocused = document.activeElement;
    let year = 'all';
    let month = 'all';
    let monthBuckets = [];
    let yearsList = [];
    let pvg = null;

    const closeBtn = h('md-icon-button', { 'aria-label': 'Chiudi selettore', title: 'Chiudi', onclick: () => close() }, icon('close'));
    const confirmBtn = h('md-filled-button', { class: 'album-picker__confirm', onclick: () => submit() }, 'Aggiungi');
    confirmBtn.disabled = true;
    const title = h('h2', { id: 'album-picker-title', class: 'album-picker__title md-typescale-title-large' }, 'Aggiungi foto');
    const yearsHost = h('div', { class: 'chip-row album-picker__chips' });
    const monthsHost = h('div', { class: 'chip-row album-picker__chips' });
    const body = h('div', { class: 'album-picker__body' });
    const dlg = h('div', {
      class: 'album-picker', role: 'dialog', 'aria-modal': 'true', 'aria-labelledby': 'album-picker-title',
    },
      h('header', { class: 'album-picker__bar' }, closeBtn, title, confirmBtn),
      h('div', { class: 'album-picker__filters' }, yearsHost, monthsHost),
      body);
    document.body.append(dlg);
    picker = { destroy: close };
    // Esc e Tab in fase di cattura su window: Esc chiude solo il selettore; Tab resta dentro il dialogo (aria-modal).
    const focusables = () => [...dlg.querySelectorAll('md-icon-button, md-filled-button, md-filter-chip, .photo-tile[tabindex="0"]')]
      .filter((el) => !el.disabled && el.getClientRects().length);
    const onKey = (e) => {
      if (e.key === 'Escape') {
        e.preventDefault();
        e.stopPropagation();
        close();
        return;
      }
      if (e.key !== 'Tab') return;
      const f = focusables();
      if (!f.length) return;
      const cur = document.activeElement;
      const inside = dlg.contains(cur);
      const first = f[0];
      const last = f[f.length - 1];
      if (e.shiftKey ? (!inside || cur === first) : (!inside || cur === last)) {
        e.preventDefault();
        (e.shiftKey ? last : first).focus();
      }
    };
    window.addEventListener('keydown', onKey, true);
    // Il focus va sul pulsante Chiudi dentro il dialogo (dopo il montaggio dei componenti md).
    requestAnimationFrame(() => { if (dlg.isConnected) closeBtn.focus(); });

    // Resize/rotazione con il selettore aperto: anche le tile del selettore cambiano dimensione.
    const pmq = window.matchMedia?.('(max-width: 599px)');
    const onPickerMq = () => pvg?.setTileSize(tileSize());
    pmq?.addEventListener('change', onPickerMq);

    // Foto gia' nell'album: si carica l'elenco degli id (a pagine) per segnarle e non farle riselezionare.
    const memberIds = new Set();
    const membersReady = (async () => {
      let offset = 0;
      for (;;) {
        const page = await api.get(`${path}/files`, { offset, limit: 1000 }, pac.signal);
        for (const f of page.items) memberIds.add(f.id);
        offset += page.items.length;
        if (!page.items.length || offset >= page.total) break;
      }
    })().catch((e) => { if (e?.name !== 'AbortError') fail(e); });

    const syncCount = () => {
      confirmBtn.textContent = psel.count ? `Aggiungi ${formatNumber(psel.count)}` : 'Aggiungi';
      confirmBtn.disabled = psel.count === 0;
    };
    const unsubPsel = psel.subscribe(() => {
      for (const t of mountedTiles(body)) updateTile(t, { selected: psel.has(Number(t.dataset.id)) });
      syncCount();
    });

    const filterParams = () => {
      const p = {};
      if (year !== 'all') p.year = year;
      if (year !== 'all' && month !== 'all') p.month = month;
      return p;
    };

    const makePickerTile = (file) => {
      const member = memberIds.has(file.id);
      const tile = createTile(file, {
        size: tileSize(),
        selectable: !member,
        selected: psel.has(file.id),
        onOpen: () => { if (!member) psel.toggle(file.id); },
        onSelectToggle: (f, on) => { if (!member) (on ? psel.add(f.id) : psel.remove(f.id)); },
      });
      tile.dataset.id = file.id;
      if (member) {
        tile.classList.add('is-member');
        tile.setAttribute('aria-disabled', 'true');
        tile.append(h('span', { class: 'album-picker__member', 'aria-hidden': 'true' }, "Già nell'album"));
      }
      return tile;
    };

    const reloadPicker = () => {
      if (!pvg) {
        pvg = new VirtualGrid(body, {
          tile: tileSize(),
          gap: 4,
          pageSize: PAGE,
          fetchPage: (offset, limit, sig) => api.get('/files', { offset, limit, sort: 'date_desc', ...filterParams() }, sig ?? pac.signal),
          renderItem: (file) => makePickerTile(file),
        });
        body.addEventListener('error', onGridError);
      }
      pvg.reload();
    };

    const renderMonths = () => {
      monthsHost.replaceChildren();
      if (year === 'all') return;
      const months = monthBuckets
        .filter((b) => String(b.year) === year)
        .sort((a, b) => a.month - b.month)
        .map((b) => ({ id: String(b.month), label: cap(mesiNomi[b.month - 1] || String(b.month)) }));
      if (!months.length) return;
      monthsHost.append(filterChips([{ id: 'all', label: 'Tutti i mesi' }, ...months], {
        selected: month,
        label: 'Filtra per mese',
        onChange: (id) => { month = id ?? 'all'; reloadPicker(); },
      }));
    };

    const renderYears = () => {
      yearsHost.replaceChildren(filterChips(
        [{ id: 'all', label: 'Tutti gli anni' }, ...yearsList.map((y) => ({ id: String(y.year), label: String(y.year) }))],
        {
          selected: year,
          label: 'Filtra per anno',
          onChange: (id) => {
            year = id ?? 'all';
            month = 'all';
            renderMonths();
            reloadPicker();
          },
        },
      ));
    };

    Promise.all([
      api.get('/timeline/years', undefined, pac.signal),
      api.get('/timeline/buckets', { granularity: 'month' }, pac.signal),
      membersReady,
    ]).then(([years, buckets]) => {
      if (!dlg.isConnected) return;
      yearsList = Array.isArray(years) ? years : [];
      monthBuckets = (buckets?.buckets || []).filter((b) => b.year != null && b.month != null);
      renderYears();
      renderMonths();
      reloadPicker();
    }).catch((e) => {
      if (!dlg.isConnected || e?.name === 'AbortError') return;
      fail(e);
      reloadPicker();
    });

    async function submit() {
      const ids = [...psel.ids];
      if (!ids.length) return;
      confirmBtn.disabled = true;
      try {
        const res = await api.post(`${path}/items`, { file_ids: ids });
        close();
        let msg = res.added ? plural(res.added, 'foto aggiunta', 'foto aggiunte') : "Le foto erano già nell'album";
        if (res.added && res.already_present) msg += `, ${formatNumber(res.already_present)} già presenti`;
        showSnackbar(msg);
        if (alive) await loadAlbum();
      } catch (e) {
        if (dlg.isConnected) confirmBtn.disabled = psel.count === 0;
        fail(e);
      }
    }

    function close() {
      if (!dlg.isConnected) return;
      picker = null;
      window.removeEventListener('keydown', onKey, true);
      pmq?.removeEventListener('change', onPickerMq);
      pac.abort();
      unsubPsel();
      pvg?.destroy();
      pvg = null;
      dlg.remove();
      if (previouslyFocused?.isConnected) previouslyFocused.focus?.();
      else addBtn.focus?.();
    }
  }

  // ---------- Pulizia ----------
  function disposeGridParts() {
    scroller?.removeEventListener('error', onGridError);
    scroller = null;
    vg?.destroy();
    vg = null;
    bar?.destroy();
    bar = null;
    unsubSel?.();
    unsubSel = null;
    selection.clear();
  }

  function disposeGrid() {
    disposeGridParts();
    mq?.removeEventListener('change', onMq);
    mq = null;
  }

  loadAlbum();

  return () => {
    alive = false;
    ac.abort();
    ctx.signal?.removeEventListener('abort', onCtxAbort);
    picker?.destroy?.();
    picker = null;
    disposeGrid();
  };
}
