// Ricerca semantica in linguaggio naturale (route '/search', ?q=). Stili: css/view-search.css.
// I risultati sono virtualizzati con VirtualGrid: lo scroll e' quello della view (.view-outlet),
// quindi con migliaia di risultati nel DOM restano solo le righe visibili.
import { h, icon } from '../ui/el.js';
import { api } from '../api.js';
import { showSnackbar } from '../ui/snackbar.js';
import { emptyState, errorState } from '../ui/empty-state.js';
import { createTile } from '../ui/photo-tile.js';
import { filterChips } from '../ui/chips.js';
import { createSelection, selectionBar } from '../ui/selection.js';
import { albumPickerDialog } from '../ui/dialog.js';
import { plural } from '../ui/format.js';
import { VirtualGrid } from '../ui/virtual-grid.js';

const PAGE = 60;
const DEBOUNCE_MS = 350;
const SETTLE_MS = 1500;           // la query entra nella cronologia quando si ferma e ha dei risultati
const STATUS_POLL_MS = 2000;
const MAX_RETRY = 12;             // tentativi automatici sui 503 (modello in preparazione)
const RECENT_KEY = 'fotoscan.search.recent';
const RECENT_MAX = 8;
const TILE = 180;
const MSG_UNAVAILABLE = 'La ricerca non è disponibile al momento. Riprova tra poco.';
const MAX_QUERY = 200;           // come il backend (routes_search.py): il campo non accetta di piu'
const EXAMPLES = ['tramonto in montagna', 'cane sul divano', 'scontrino'];
// Le corrispondenze "possibile" non hanno etichetta sulla tile: stanno nella sezione "risultati incerti" in fondo.
const LEVEL_LABEL = { ottima: 'Ottima', buona: 'Buona' };
const LEVEL_TITLE = {
  ottima: 'Ottima corrispondenza con la ricerca',
  buona: 'Buona corrispondenza con la ricerca',
};

const sleep = (ms, signal) => new Promise((resolve, reject) => {
  const t = setTimeout(resolve, ms);
  signal.addEventListener('abort', () => { clearTimeout(t); reject(new DOMException('Aborted', 'AbortError')); }, { once: true });
});

function loadCss() {
  if (document.querySelector('link[data-view="search"]')) return;
  const link = h('link', { rel: 'stylesheet', href: 'css/view-search.css' });
  link.dataset.view = 'search';
  document.head.append(link);
}

// Ricerche recenti: localStorage, sempre in try/catch (puo' essere disabilitato).
function readRecent() {
  try {
    const list = JSON.parse(localStorage.getItem(RECENT_KEY) || '[]');
    return Array.isArray(list) ? list.filter((s) => typeof s === 'string') : [];
  } catch { return []; }
}
function writeRecent(list) {
  try {
    if (list.length) localStorage.setItem(RECENT_KEY, JSON.stringify(list));
    else localStorage.removeItem(RECENT_KEY);
  } catch { /* memoria piena o bloccata: la cronologia resta solo per questa sessione */ }
}

/** Aggiorna l'URL senza ricaricare la view (replaceState non genera hashchange). */
function syncUrl(q) {
  const hash = q ? `#/search?${new URLSearchParams({ q }).toString()}` : '#/search';
  if (location.hash !== hash) history.replaceState(null, '', hash);
}

/** Campo della barra in alto (shell.js): tenuto allineato con il campo della pagina. */
const shellField = () => document.querySelector('.topbar-search-input');  // classe reale in shell.js

export async function mount(root, ctx) {
  loadCss();
  const selection = createSelection();
  const ac = new AbortController();           // richieste di sezione (suggerimenti, stato modello)
  ctx.signal?.addEventListener('abort', () => ac.abort(), { once: true });

  let disposed = false;
  let seq = 0;                                // identifica la ricerca corrente
  let q = '', kind = '';
  let results = [];                           // File per indice (sparso) della ricerca corrente, per il visualizzatore
  let grid = null;
  let pollTimer = null, debounceTimer = null, settleTimer = null;
  let preparing = false;
  let modelReady = false;                     // dopo il primo successo non si ricontrolla
  let errorShown = false;
  let showUncertain = false;                  // mostra anche i risultati "possibile" (scelta dell'utente)
  let uncertainCount = 0;                     // quanti risultati incerti ha la ricerca corrente

  // ---------------------------------------------------------------- DOM statico
  let filtersOpen = false;
  const field = h('input', {
    class: 'search-input', type: 'search', placeholder: 'Descrivi una foto o un video… es. «tramonto in montagna»',
    'aria-label': 'Cerca nelle foto', autocomplete: 'off', spellcheck: false, maxlength: MAX_QUERY,
  });
  const clearBtn = h('md-icon-button', {
    class: 'search-clear', 'aria-label': 'Cancella ricerca', title: 'Cancella ricerca',
    onclick: () => clearSearch(),
  }, icon('close'));
  clearBtn.style.display = 'none';

  const filterBtnLabel = h('span', { class: 'search-filter-btn__label' }, 'Filtri');
  const filterToggleBtn = h('button', {
    type: 'button', class: 'search-filter-btn',
    'aria-expanded': 'false', 'aria-controls': 'search-filter-drawer',
    title: 'Filtra per foto o video',
    onclick: () => setFiltersOpen(!filtersOpen),
  }, icon('tune'), filterBtnLabel);

  const pill = h('div', { class: 'search-pill' },
    h('span', { class: 'search-pill__icon', 'aria-hidden': 'true' }, icon('search')),
    field,
    clearBtn,
    h('span', { class: 'search-pill__sep', 'aria-hidden': 'true' }),
    filterToggleBtn);

  const KIND_LABELS = { '': 'Filtri', image: 'Solo foto', video: 'Solo video' };
  const chips = filterChips([
    { id: 'all', label: 'Tutti', icon: 'apps' },
    { id: 'image', label: 'Solo foto', icon: 'photo' },
    { id: 'video', label: 'Solo video', icon: 'videocam' },
  ], {
    selected: 'all', label: 'Tipo di file',
    onChange: (id) => {
      kind = id === 'all' ? '' : id;
      syncFilterButton();
      if (q) runSearch(q);
    },
  });

  const filterDrawer = h('div', { id: 'search-filter-drawer', class: 'search-filter-drawer', 'aria-hidden': 'true' },
    h('div', { class: 'search-filter-drawer__inner' },
      h('div', { class: 'search-filter-card' },
        h('span', { class: 'search-filter-card__label md-typescale-label-medium' }, 'Mostra'),
        chips)));

  function syncFilterButton() {
    const active = !!kind;
    filterToggleBtn.classList.toggle('is-active', active || filtersOpen);
    filterToggleBtn.classList.toggle('has-filter', active);
    filterBtnLabel.textContent = KIND_LABELS[kind] || 'Filtri';
  }
  function setFiltersOpen(on) {
    filtersOpen = !!on;
    filterToggleBtn.setAttribute('aria-expanded', String(filtersOpen));
    filterDrawer.classList.toggle('is-open', filtersOpen);
    filterDrawer.setAttribute('aria-hidden', String(!filtersOpen));
    syncFilterButton();
  }

  const fieldWrap = h('div', { class: 'search-field-wrap', role: 'search' }, pill, filterDrawer);

  const banner = h('div', { class: 'callout callout--info search-banner', role: 'status', hidden: true },
    h('md-linear-progress', { indeterminate: true, 'aria-hidden': 'true' }),
    h('p', { class: 'md-typescale-body-medium' }, 'Sto preparando la ricerca, la prima volta ci vuole qualche secondo'));

  const sugBox = h('div', { class: 'search-chips', role: 'group', 'aria-label': 'Suggerimenti' });
  const recentBox = h('div', { class: 'search-chips', role: 'group', 'aria-label': 'Ricerche recenti' });
  const recentHead = h('div', { class: 'search-group-head' },
    h('h2', { class: 'md-typescale-title-small' }, 'Ricerche recenti'),
    h('md-text-button', { onclick: () => clearHistory() }, 'Cancella cronologia'));
  const recentGroup = h('div', { class: 'search-group search-card' }, recentHead, recentBox);

  const initial = h('section', { class: 'search-initial', 'aria-label': 'Suggerimenti di ricerca' },
    h('div', { class: 'search-group search-card' },
      h('h2', { class: 'md-typescale-title-small' }, 'Idee di ricerca'),
      sugBox),
    recentGroup);

  const summary = h('p', { class: 'search-summary md-typescale-body-medium', 'aria-live': 'polite' });
  const moreBtn = h('md-text-button', { class: 'search-more-btn', onclick: () => toggleUncertain() });
  const moreBox = h('div', { class: 'search-more', hidden: true }, moreBtn);
  const status = h('div', { class: 'search-status' });   // stato vuoto / errore / in attesa
  const barHost = h('div', { class: 'search-selection-host' });
  const selBar = selectionBar(barHost, {
    selection,
    actions: [{
      id: 'album', icon: 'photo_album', label: 'Aggiungi ad album',
      run: async (ids) => {
        const done = await albumPickerDialog({ fileIds: ids });
        if (done) selection.clear();
      },
    }],
  });

  const resultsHost = h('div', { class: 'search-results' }, summary, status);
  const view = h('section', { class: 'view search-view' },
    h('header', { class: 'view-header search-hero' },
      h('div', { class: 'search-hero__inner' },
        h('h1', { class: 'search-hero__title' }, 'Cerca nelle foto'),
        h('p', { class: 'search-hero__sub md-typescale-body-medium' },
          `Scrivi con parole naturali quello che ricordi, come ${EXAMPLES.map((e) => `«${e}»`).join(', ')}.`))),
    fieldWrap, banner,
    barHost, initial, resultsHost);
  root.append(view);

  // Cambio di selezione: si ridisegnano solo le righe montate (la griglia resta virtuale).
  const unsubSelection = selection.subscribe(() => {
    if (grid && !grid.destroyed) { grid.unmountAll(); grid.update(); }
  });

  // ---------------------------------------------------------------- stato del modello
  function setPreparing(on) {
    if (preparing === on) return;
    preparing = on;
    banner.hidden = !on;
    clearInterval(pollTimer);
    pollTimer = null;
    if (on) {
      pollTimer = setInterval(async () => {
        if (document.hidden) return;   // scheda nascosta: riprova quando torna visibile
        try {
          const s = await api.get('/search/status', undefined, ac.signal);
          // Pronto, oppure caricamento fallito: il banner non deve restare su "sto preparando".
          if (!disposed && (s.model_loaded || s.error)) setPreparing(false);
        } catch { /* riprova al ciclo successivo */ }
      }, STATUS_POLL_MS);
    }
  }

  async function checkModel() {
    try {
      const s = await api.get('/search/status', undefined, ac.signal);
      if (disposed || s.model_loaded) return;
      setPreparing(true);
      await api.post('/search/warmup');
    } catch (err) {
      if (!disposed && err.name !== 'AbortError') showSnackbar(err.message, { error: true });
    }
  }

  // ---------------------------------------------------------------- richiesta di una pagina
  /** Attende il modello controllando lo stato (niente richieste che fallirebbero con 503 nel log). */
  async function waitModel(signal) {
    for (let attempt = 0; attempt < 240; attempt++) {
      const s = await api.get('/search/status', undefined, signal);
      if (s.model_loaded) { modelReady = true; setPreparing(false); return; }
      if (s.error) {
        // Dettaglio tecnico solo in console: l'utente vede un messaggio in italiano chiaro.
        setPreparing(false);
        console.warn('[ricerca] modello non disponibile:', s.error);
        throw new Error(MSG_UNAVAILABLE);
      }
      setPreparing(true);
      if (!s.loading) await api.post('/search/warmup').catch(() => {});
      await sleep(1000, signal);
    }
    throw new Error('La preparazione della ricerca sta impiegando troppo. Riprova tra poco.');
  }

  /** Una pagina di risultati; se il modello non e' ancora pronto aspetta, poi chiede la pagina. */
  async function searchPage(text, k, offset, limit, signal, incerti = false) {
    if (!modelReady) await waitModel(signal);
    for (let attempt = 0; ; attempt++) {
      try {
        const data = await api.get('/search', { q: text, limit, offset, kind: k, incerti: incerti ? 1 : 0 }, signal);
        setPreparing(false);
        return data;
      } catch (err) {
        if (err.name === 'AbortError' || disposed) throw err;
        if (err.status === 503 && attempt < MAX_RETRY) {
          setPreparing(true);
          await sleep(Math.min(1000 * 2 ** attempt, 8000), signal);
          continue;
        }
        if (err.status === 503) throw new Error(MSG_UNAVAILABLE);
        throw err;
      }
    }
  }

  // ---------------------------------------------------------------- ricerca
  function destroyGrid() {
    if (grid) { grid.destroy(); grid = null; }  // annulla anche le pagine in volo
    moreBox.hidden = true;
    root.removeEventListener('empty', onEmpty);
    root.removeEventListener('error', onGridError);
  }

  function showStatus(node) {
    status.replaceChildren(...(node ? [node] : []));
  }

  /** Pulsante sotto la griglia: mostra o nasconde i risultati "possibile" (incerti). */
  function updateMore() {
    moreBox.hidden = !(grid && uncertainCount > 0);
    moreBtn.textContent = showUncertain
      ? 'Nascondi i risultati incerti'
      : `Mostra ${plural(uncertainCount, 'risultato incerto', 'risultati incerti')}`;
  }

  function toggleUncertain() {
    showUncertain = !showUncertain;
    runSearch(q);
  }

  function runSearch(text) {
    destroyGrid();
    const my = ++seq;
    q = text;
    results = [];
    errorShown = false;
    uncertainCount = 0;
    selection.clear();
    showInitial(false);
    syncUrl(q);
    setShellQuery(q);
    summary.textContent = `Ricerca di «${q}» in corso…`;
    showStatus(null);
    updateMore();

    const fetchPage = async (offset, limit, signal) => {
      const data = await searchPage(text, kind, offset, limit, signal, showUncertain);
      if (my !== seq) throw new DOMException('Aborted', 'AbortError');
      data.items.forEach((f, i) => { results[offset + i] = f; });
      if (offset === 0) {
        summary.textContent = data.total ? `${plural(data.total, 'risultato', 'risultati')} per «${q}»` : '';
        uncertainCount = data.uncertain || 0;
        updateMore();
      }
      return data;
    };

    grid = new VirtualGrid(root, {
      fetchPage,
      renderItem: (file) => renderTile(file),
      tile: TILE,
      gap: 4,
      pageSize: PAGE,
    });
    root.append(moreBox);  // dopo la griglia: il pulsante sta in fondo ai risultati
    root.addEventListener('empty', onEmpty);
    root.addEventListener('error', onGridError);
    armRecent(text);
    grid.reload();
  }

  /** Nessuna corrispondenza sicura: stato vuoto con consigli (la griglia resta a altezza zero). */
  function onEmpty() {
    summary.textContent = '';
    const incerti = uncertainCount > 0 && !showUncertain
      ? `La ricerca ha trovato ${plural(uncertainCount, 'risultato incerto', 'risultati incerti')}: usa il pulsante in fondo per vederli.`
      : 'La ricerca mostra solo le corrispondenze sufficienti: se la foto c\'è, descrivila con altre parole.';
    showStatus(emptyState({
      icon: 'image_search',
      title: `Nessuna foto trovata per «${q}»`,
      message: [
        'Prova parole più semplici o più descrittive, per esempio «mare», «cane» o «tramonto in montagna».',
        kind ? 'Il filtro Foto/Video può nascondere dei risultati: prova a toglierlo.' : '',
        incerti,
      ].filter(Boolean).join(' '),
      actionLabel: 'Cancella ricerca',
      onAction: () => clearSearch(),
    }));
  }

  function onGridError(e) {
    const { error, offset } = e.detail || {};
    if (error?.name === 'AbortError') return;
    const message = error?.message || 'Errore durante la ricerca';
    if (offset === 0 || offset === undefined) {
      summary.textContent = '';
      showStatus(errorState({ title: 'Ricerca non riuscita', message, onRetry: () => runSearch(q) }));
      return;
    }
    if (!errorShown) showSnackbar(message, { error: true });  // una sola segnalazione per ricerca
    errorShown = true;
  }

  /** Una tile dei risultati, con la fascia di corrispondenza (non una percentuale). */
  function renderTile(file) {
    const tile = createTile(file, {
      size: TILE,
      selectable: true,
      selected: selection.has(file.id),
      showMeta: true,
      onOpen: (f) => onOpen(f),
      onSelectToggle: (f, on) => (on ? selection.add(f.id) : selection.remove(f.id)),
    });
    const level = file.match_level;
    if (LEVEL_LABEL[level]) {
      tile.append(h('span', {
        class: `search-score search-score--${level}`, title: LEVEL_TITLE[level],
      }, LEVEL_LABEL[level]));
    }
    return tile;
  }

  async function onOpen(file) {
    if (selection.count > 0) {
      selection.toggle(file.id);
      return;
    }
    // Il visualizzatore riceve la sequenza di risultati caricati finora, poi carica le pagine successive
    // (solo finche' la ricerca e' la stessa: se ne parte un'altra, il visualizzatore non ottiene piu' nulla).
    const my = seq;
    const items = [];
    for (let i = 0; i < results.length && results[i]; i++) items.push(results[i]);
    const index = items.findIndex((f) => f.id === file.id);
    let loaded = items.length;
    const loadMore = async () => {
      if (disposed || my !== seq || (grid && grid.total !== null && loaded >= grid.total)) return [];
      const from = loaded;
      const data = await searchPage(q, kind, from, PAGE, new AbortController().signal, showUncertain);
      data.items.forEach((f, i) => { results[from + i] = f; });
      loaded = from + data.items.length;
      return data.items;
    };
    try {
      const { openViewer } = await import('./viewer.js');
      if (disposed) return;
      openViewer({
        items, index: Math.max(index, 0), loadMore,
        onDelete: () => { if (!disposed && q) runSearch(q); },
      });
    } catch (err) {
      showSnackbar(`Impossibile aprire la foto: ${err.message}`, { error: true });
    }
  }

  // ---------------------------------------------------------------- barra e pagina allineate
  /** Query della pagina -> barra in alto (solo testo: la ricerca parte da Invio o dalla pausa). */
  function setShellQuery(text) {
    const sf = shellField();
    if (sf && sf.value !== text) sf.value = text;
  }

  function showInitial(on) {
    initial.hidden = !on;
    view.classList.toggle('is-results', !on);
    if (on) { summary.textContent = ''; showStatus(null); }
  }

  function runNow(raw) {
    clearTimeout(debounceTimer);
    const text = (raw || '').replace(/\s+/g, ' ').trim();
    if (!text) {
      destroyGrid();
      seq++;
      q = '';
      results = [];
      selection.clear();
      syncUrl('');
      setShellQuery('');
      showInitial(true);
      renderRecent();
      return;
    }
    if (text === q && grid) return;
    showUncertain = false;  // una nuova ricerca parte sempre dai soli risultati sicuri
    runSearch(text);
  }

  function clearSearch() {
    field.value = '';
    clearBtn.style.display = 'none';
    field.focus();
    runNow('');
  }

  function scheduleSearch(value) {
    clearBtn.style.display = value ? '' : 'none';
    clearTimeout(debounceTimer);
    debounceTimer = setTimeout(() => runNow(value), DEBOUNCE_MS);
  }

  field.addEventListener('input', () => {
    setShellQuery(field.value);
    scheduleSearch(field.value);
  });
  field.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') {
      e.preventDefault();
      runNow(field.value);  // la cronologia la decide armRecent(): solo ricerche con risultati
    } else if (e.key === 'Escape') {
      e.preventDefault();
      if (field.value) clearSearch(); else field.blur();
    } else if (e.key === 'ArrowDown') {
      const first = sugBox.querySelector('md-assist-chip') || recentBox.querySelector('md-assist-chip');
      if (first && !initial.hidden) { e.preventDefault(); first.focus(); }
    }
  });

  // Barra in alto: digitare li' aggiorna il campo della pagina (la ricerca parte come qui).
  const sf = shellField();
  const onShellInput = () => {
    if (!sf) return;
    field.value = sf.value;
    clearBtn.style.display = field.value ? '' : 'none';
    scheduleSearch(field.value);
  };
  sf?.addEventListener('input', onShellInput);

  // Frecce fra i suggerimenti e le recenti; Esc torna al campo.
  const onChipKey = (e) => {
    const chipsNow = [...e.currentTarget.querySelectorAll('md-assist-chip')];
    const i = chipsNow.indexOf(e.target.closest('md-assist-chip'));
    if (i < 0) return;
    const move = { ArrowRight: 1, ArrowDown: 1, ArrowLeft: -1, ArrowUp: -1 }[e.key];
    if (move) {
      e.preventDefault();
      chipsNow[(i + move + chipsNow.length) % chipsNow.length].focus();
    } else if (e.key === 'Escape') {
      e.preventDefault();
      field.focus();
    }
  };
  sugBox.addEventListener('keydown', onChipKey);
  recentBox.addEventListener('keydown', onChipKey);

  // ---------------------------------------------------------------- suggerimenti e cronologia
  /** Ricerca che si e' fermata: entra in cronologia solo se ha dei risultati. */
  function armRecent(text) {
    clearTimeout(settleTimer);
    settleTimer = setTimeout(() => {
      if (!disposed && text === q && grid?.total) rememberRecent(text);
    }, SETTLE_MS);
  }

  function rememberRecent(text) {
    if (!text) return;
    const key = text.toLowerCase();
    const list = [text, ...readRecent().filter((s) => s.toLowerCase() !== key)].slice(0, RECENT_MAX);
    writeRecent(list);
    renderRecent();
  }

  function clearHistory() {
    writeRecent([]);
    renderRecent();
  }

  function renderRecent() {
    const list = readRecent();
    recentGroup.hidden = !list.length;
    recentBox.replaceChildren(...list.map((s) => chip(s, 'history')));
  }

  function chip(text, iconName) {
    return h('md-assist-chip', {
      label: text,
      onclick: () => {
        field.value = text;
        setShellQuery(text);
        clearBtn.style.display = '';
        runNow(text);  // come sopra: nessuna cronologia diretta
      },
    }, icon(iconName, { slot: 'icon' }));
  }

  api.get('/search/suggestions', undefined, ac.signal)
    .then((list) => {
      if (disposed) return;
      sugBox.replaceChildren(...list.map((s) => chip(s, 'auto_awesome')));
    })
    .catch((err) => {
      if (!disposed && err.name !== 'AbortError') showSnackbar(err.message, { error: true });
    });

  // ---------------------------------------------------------------- avvio
  renderRecent();
  const initialQ = (ctx.query?.q || '').replace(/\s+/g, ' ').trim();
  field.value = initialQ;
  clearBtn.style.display = initialQ ? '' : 'none';
  setShellQuery(initialQ);
  checkModel();
  requestAnimationFrame(() => field.focus());
  if (initialQ) runNow(initialQ);
  else showInitial(true);

  // ---------------------------------------------------------------- cleanup
  return () => {
    disposed = true;
    seq++;
    destroyGrid();
    ac.abort();
    clearTimeout(debounceTimer);
    clearTimeout(settleTimer);
    clearInterval(pollTimer);
    sf?.removeEventListener('input', onShellInput);
    unsubSelection();
    selBar.destroy();
    view.remove();
    moreBox.remove();
  };
}
