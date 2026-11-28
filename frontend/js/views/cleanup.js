// Libera spazio: pulizia guidata di video pesanti, screenshot, documenti e foto pesanti
// con griglia raggruppata Per tempo (mese/anno), Per contesto (etichetta o cartella) o Più pesanti,
// selezione per gruppo, tasto Cestino rapido su ogni card e barra sospesa in basso.
import { h, icon } from '../ui/el.js';
import { api } from '../api.js';
import { showSnackbar } from '../ui/snackbar.js';
import { confirmDialog } from '../ui/dialog.js';
import { createTile, updateTile } from '../ui/photo-tile.js';
import { createSelection, useRangeSelect } from '../ui/selection.js';
import { skeletonGrid } from '../ui/skeleton.js';
import { emptyState, errorState } from '../ui/empty-state.js';
import { formatBytes, formatMonth, formatNumber, plural, missingNote } from '../ui/format.js';

const CATEGORIES = [
  { id: 'large_videos', icon: 'videocam',                title: 'Video pesanti',         hint: 'Video da almeno 50 MB' },
  { id: 'screenshots',  icon: 'screenshot',              title: 'Screenshot',            hint: 'Schermate salvate nel tempo' },
  { id: 'documents',    icon: 'receipt_long',            title: 'Documenti e scontrini', hint: 'Ricevute, fogli e appunti' },
  { id: 'large_photos', icon: 'photo_size_select_large', title: 'Foto pesanti',          hint: 'Foto da almeno 8 MB' },
];

const GROUP_MODES = [
  { id: 'time',    icon: 'calendar_month', label: 'Per tempo' },
  { id: 'context', icon: 'folder_special', label: 'Per contesto' },
  { id: 'size',    icon: 'weight',         label: 'Più pesanti' },
];

const PAGE = 120;
const CSS_URL = new URL('../../css/view-cleanup.css', import.meta.url).href;
const cap = (s) => (s ? s.charAt(0).toUpperCase() + s.slice(1) : '');

function loadCss() {
  if (document.querySelector('link[data-view-css="cleanup"]')) return;
  document.head.append(h('link', { rel: 'stylesheet', href: CSS_URL, dataset: { viewCss: 'cleanup' } }));
}

function parentFolderName(path) {
  if (!path) return 'Altra cartella';
  const parts = String(path).split(/[\\/]+/).filter(Boolean);
  if (parts.length < 2) return 'Cartella principale';
  return parts[parts.length - 2];
}

function timeGroupKey(file) {
  const d = file?.date;
  if (!d || d.length < 7 || !/^\d{4}-\d{2}/.test(d)) return { key: 'unknown', label: 'Senza data' };
  const y = Number(d.slice(0, 4));
  const m = Number(d.slice(5, 7));
  return { key: `${y}-${String(m).padStart(2, '0')}`, label: cap(formatMonth(y, m)) };
}

function contextGroupKey(file, catId) {
  const ctx = (file?.context || '').trim();
  const isGenericCat = (catId === 'screenshots' && ctx === 'Screenshot')
    || (catId === 'documents' && ctx === 'Documenti e scontrini');
  if (ctx && !isGenericCat) return { key: `ctx:${ctx.toLowerCase()}`, label: ctx };
  const folder = parentFolderName(file?.path);
  return { key: `dir:${folder.toLowerCase()}`, label: folder };
}

export async function mount(root, ctx) {
  loadCss();
  let alive = true;
  const catId = CATEGORIES.some((c) => c.id === ctx.query?.cat) ? ctx.query.cat : 'large_videos';
  const catInfo = CATEGORIES.find((c) => c.id === catId);

  let groupMode = 'time'; // 'time' | 'context' | 'size'
  let timeAsc = false;    // false = piu' recenti prima, true = piu' vecchi prima
  let loadSeq = 0;

  const selection = createSelection();
  let items = [];
  let total = 0;
  let totalBytes = 0;
  let offset = 0;
  let loadingMore = false;
  let io = null;
  const tileMap = new Map(); // file_id -> tileEl
  let lastShift = false;
  const rangeSelect = useRangeSelect(selection, () => items.map((f) => f.id));

  // ---------- Controlli superiori (compatti, 1 sola riga) ----------
  const modeBtns = new Map();
  const modeSwitcher = h('div', { class: 'cln-modes', role: 'tablist', 'aria-label': 'Raggruppamento' },
    GROUP_MODES.map((m) => {
      const btn = h('button', {
        type: 'button', role: 'tab', class: 'cln-mode-btn',
        onclick: () => setGroupMode(m.id),
      }, icon(m.icon), h('span', {}, m.label));
      modeBtns.set(m.id, btn);
      return btn;
    }));

  const orderBtn = h('md-text-button', {
    onclick: () => {
      timeAsc = !timeAsc;
      renderControls();
      reloadItems();
    },
  }, icon('swap_vert', { slot: 'icon' }), 'Più recenti');

  const countBadge = h('span', { class: 'muted md-typescale-body-small cln-count-badge' }, 'Caricamento…');

  const selectAllBtn = h('md-text-button', {
    onclick: () => {
      if (selection.count >= items.length && items.length > 0) selection.clear();
      else selection.selectAll(items.map((f) => f.id));
    },
  }, 'Seleziona visibili');

  const controlsBar = h('div', { class: 'cln-controls' },
    h('div', { class: 'cln-controls__left' }, modeSwitcher, orderBtn),
    h('div', { class: 'cln-controls__right' }, countBadge, selectAllBtn));

  // ---------- Area contenuto ----------
  const contentHost = h('div', { class: 'cln-groups' });
  contentHost.addEventListener('click', (e) => { lastShift = e.shiftKey; }, true);
  // L'apertura da tastiera (Invio) non e' un click: non deve ereditare lo Shift dell'ultimo clic.
  contentHost.addEventListener('keydown', () => { lastShift = false; }, true);

  const moreBtn = h('md-outlined-button', { hidden: true, onclick: () => loadMore(loadSeq) }, 'Carica altri');
  const moreWrap = h('div', { class: 'cln-more' }, moreBtn);
  const sentinel = h('div', { style: 'height:1px;' });

  // ---------- Barra sospesa in basso (solo quando c'e' selezione) ----------
  const floatInfo = h('strong', { class: 'md-typescale-body-medium' });
  const floatTrashBtn = h('md-filled-button', {
    class: 'cln-trash-btn',
    onclick: () => trashIds([...selection.ids]),
  }, icon('delete', { slot: 'icon' }), 'Sposta nel Cestino');
  const floatBar = h('div', { class: 'cln-float-bar', role: 'region', 'aria-label': 'Azioni sui file selezionati', hidden: true },
    floatInfo,
    h('div', { class: 'cln-float-bar__actions' },
      floatTrashBtn,
      h('md-text-button', { onclick: () => selection.clear() }, 'Deseleziona')));

  const view = h('div', { class: 'view cln-view' }, controlsBar, contentHost, moreWrap, sentinel, floatBar);
  root.replaceChildren(view);

  function apiSort() {
    if (groupMode === 'size') return 'size';
    if (groupMode === 'context') return 'context';
    return timeAsc ? 'date_asc' : 'date_desc';
  }

  function renderControls() {
    for (const m of GROUP_MODES) {
      const btn = modeBtns.get(m.id);
      const on = m.id === groupMode;
      btn.classList.toggle('is-active', on);
      btn.setAttribute('aria-selected', String(on));
    }
    orderBtn.hidden = groupMode !== 'time';
    orderBtn.replaceChildren(
      icon('swap_vert', { slot: 'icon' }),
      timeAsc ? 'Più vecchi' : 'Più recenti',
    );
    countBadge.textContent = total
      ? `${plural(total, 'file', 'file')} · ${formatBytes(totalBytes)}`
      : 'Nessun file';
    selectAllBtn.hidden = items.length === 0;
    selectAllBtn.textContent = selection.count && selection.count >= items.length
      ? 'Deseleziona tutti'
      : 'Seleziona visibili';
  }

  function setGroupMode(mode) {
    if (mode === groupMode) return;
    groupMode = mode;
    renderControls();
    reloadItems();
  }

  async function openViewerAt(idx) {
    try {
      const { openViewer } = await import('./viewer.js');
      openViewer({ items, index: idx });
    } catch {
      showSnackbar('Visualizzatore non disponibile.', { error: true });
    }
  }

  /** Sposta uno o piu' file nel Cestino di Windows. Per singolo file con quick=true salta la modale. */
  let trashing = false;   // una sola richiesta di cestino alla volta (doppio clic)
  async function trashIds(ids, { quick = false } = {}) {
    if (!ids.length || !alive || trashing) return;
    trashing = true;
    try {
      await trashIdsNow(ids, { quick });
    } finally {
      trashing = false;
    }
  }

  async function trashIdsNow(ids, { quick = false } = {}) {
    const byId = new Map(items.map((f) => [f.id, f]));
    let bytes = 0;
    for (const fid of ids) bytes += Number(byId.get(fid)?.size || 0);
    if (!quick) {
      const ok = await confirmDialog({
        title: `Spostare ${plural(ids.length, 'file', 'file')} nel Cestino?`,
        message: `Libererai circa ${formatBytes(bytes)}. I file finiranno nel Cestino di Windows e potrai recuperarli finché non lo svuoti.`,
        confirmLabel: 'Sposta nel Cestino',
        danger: true,
      });
      if (!ok || !alive) return;
    }
    floatTrashBtn.disabled = true;
    try {
      const r = await api.post('/cleanup/trash', { file_ids: ids, confirm: true });
      if (!alive) return;
      const failedIds = new Set((r.errors || []).map((e) => e.id));
      const removed = new Set(ids.filter((fid) => !failedIds.has(fid)));
      items = items.filter((f) => !removed.has(f.id));
      for (const fid of removed) {
        tileMap.delete(fid);
        selection.remove(fid);
      }
      total = Math.max(0, total - removed.size);
      totalBytes = Math.max(0, totalBytes - (r.freed_bytes || 0));
      offset = Math.max(0, offset - removed.size);
      if (r.trashed) {
        showSnackbar(`${plural(r.trashed, 'file spostato', 'file spostati')} nel Cestino (${formatBytes(r.freed_bytes)} liberati).`);
        window.dispatchEvent(new CustomEvent('fotoscan:cleanup-changed'));
      }
      if (r.missing) showSnackbar(missingNote(r.missing));
      if (r.errors?.length) showSnackbar(`${plural(r.errors.length, 'file non spostato', 'file non spostati')}.`, { error: true });
      renderGroups();
      syncSelectionUi();
    } catch (e) {
      if (alive) showSnackbar(e.message || 'Operazione non riuscita.', { error: true });
    } finally {
      floatTrashBtn.disabled = false;
    }
  }

  function makeTileWrap(f, idx) {
    const tile = createTile(f, {
      size: 172,
      showMeta: true,
      selectable: true,
      selected: selection.has(f.id),
      onOpen: () => (selection.count > 0 || lastShift
        ? rangeSelect(f.id, { shiftKey: lastShift })
        : openViewerAt(idx)),
      onSelectToggle: (file) => rangeSelect(file.id, { shiftKey: lastShift }),
    });
    const quickTrash = h('md-icon-button', {
      class: 'cln-tile-trash',
      'aria-label': `Sposta ${f.name || 'file'} nel Cestino`,
      title: `Sposta nel Cestino (${formatBytes(f.size)})`,
      onclick: (e) => {
        e.stopPropagation();
        trashIds([f.id], { quick: true });
      },
    }, icon('delete'));
    const wrap = h('div', { class: 'cln-tile-wrap' }, tile, quickTrash);
    tileMap.set(f.id, tile);
    return wrap;
  }

  function renderGroups() {
    tileMap.clear();
    if (!items.length) {
      contentHost.replaceChildren(emptyState({
        icon: 'check_circle',
        title: `Nessun elemento in «${catInfo.title}»`,
        message: 'Categoria pulita: non ci sono altri file qui.',
      }));
      renderControls();
      return;
    }

    if (groupMode === 'size') {
      const grid = h('div', { class: 'tile-grid cln-grid' },
        ...items.map((f, idx) => makeTileWrap(f, idx)));
      contentHost.replaceChildren(grid);
      renderControls();
      return;
    }

    // Raggruppa gli elementi caricati per Tempo (mese/anno) o Contesto/Cartella
    const groups = [];
    const byKey = new Map();
    items.forEach((f, idx) => {
      const gInfo = groupMode === 'time' ? timeGroupKey(f) : contextGroupKey(f, catId);
      let g = byKey.get(gInfo.key);
      if (!g) {
        g = { key: gInfo.key, label: gInfo.label, entries: [], bytes: 0 };
        byKey.set(gInfo.key, g);
        groups.push(g);
      }
      g.entries.push({ file: f, idx });
      g.bytes += Number(f.size || 0);
    });

    const nodes = groups.map((g) => {
      const gIds = g.entries.map((e) => e.file.id);
      const allSel = gIds.every((id) => selection.has(id));
      const grpBtn = h('md-text-button', {
        class: 'cln-group__sel-btn',
        'data-group-key': g.key,
        onclick: () => {
          const everyOn = gIds.every((id) => selection.has(id));
          for (const id of gIds) selection.toggle(id, !everyOn);
        },
      }, icon(allSel ? 'deselect' : 'select_all', { slot: 'icon' }), allSel ? 'Deseleziona gruppo' : 'Seleziona gruppo');

      return h('section', { class: 'cln-group', 'data-group-key': g.key },
        h('div', { class: 'cln-group__head' },
          h('div', { class: 'cln-group__title-wrap' },
            h('h2', { class: 'cln-group__title md-typescale-title-medium' }, g.label),
            h('span', { class: 'cln-group__meta md-typescale-label-medium' },
              `${plural(g.entries.length, 'file', 'file')} · ${formatBytes(g.bytes)}`)),
          grpBtn),
        h('div', { class: 'tile-grid cln-grid' },
          ...g.entries.map((e) => makeTileWrap(e.file, e.idx))));
    });
    contentHost.replaceChildren(...nodes);
    renderControls();
  }

  function syncSelectionUi() {
    const ids = [...selection.ids];
    const byId = new Map(items.map((f) => [f.id, f]));
    let bytes = 0;
    for (const fid of ids) bytes += Number(byId.get(fid)?.size || 0);
    floatBar.hidden = ids.length === 0;
    if (ids.length) {
      floatInfo.textContent = `${plural(ids.length, 'file selezionato', 'file selezionati')} · ${formatBytes(bytes)}`;
      floatTrashBtn.replaceChildren(icon('delete', { slot: 'icon' }), `Sposta nel Cestino (${formatBytes(bytes)})`);
    }
    for (const [fid, el] of tileMap) updateTile(el, { selected: selection.has(fid) });
    // Aggiorna etichetta "Seleziona gruppo" sulle intestazioni
    contentHost.querySelectorAll('.cln-group').forEach((sec) => {
      const btn = sec.querySelector('.cln-group__sel-btn');
      if (!btn) return;
      const tiles = [...sec.querySelectorAll('.photo-tile[data-id]')];
      const allSel = tiles.length > 0 && tiles.every((t) => selection.has(Number(t.dataset.id)));
      btn.replaceChildren(icon(allSel ? 'deselect' : 'select_all', { slot: 'icon' }), allSel ? 'Deseleziona gruppo' : 'Seleziona gruppo');
    });
    renderControls();
  }

  const unsubSel = selection.subscribe(syncSelectionUi);

  function reloadItems() {
    loadSeq++;
    const my = loadSeq;
    if (io) { io.disconnect(); io = null; }
    items = [];
    offset = 0;
    total = 0;
    totalBytes = 0;
    loadingMore = false;
    selection.clear();
    contentHost.replaceChildren(skeletonGrid(12, { tile: 172 }));
    moreBtn.hidden = true;
    loadMore(my);
  }

  async function loadMore(seqId) {
    if (!alive || seqId !== loadSeq || loadingMore) return;
    if (offset > 0 && items.length >= total) return;
    loadingMore = true;
    moreBtn.disabled = true;
    try {
      const data = await api.get('/cleanup/items', {
        category: catId,
        sort: apiSort(),
        limit: PAGE,
        offset,
      }, ctx.signal);
      if (!alive || seqId !== loadSeq) return;
      const batch = data.items || [];
      total = data.total || 0;
      totalBytes = data.total_bytes || 0;
      const isFirst = offset === 0;
      items.push(...batch);
      offset += batch.length;
      renderGroups();
      if (isFirst && items.length < total) {
        io = new IntersectionObserver((entries) => {
          if (entries.some((e) => e.isIntersecting)) loadMore(seqId);
        }, { rootMargin: '400px 0px' });
        io.observe(sentinel);
      }
      moreBtn.hidden = items.length >= total || batch.length === 0;
      moreBtn.disabled = false;
      loadingMore = false;
      syncSelectionUi();
    } catch (e) {
      if (!alive || seqId !== loadSeq || e?.name === 'AbortError') return;
      loadingMore = false;
      moreBtn.disabled = false;
      if (offset === 0) {
        contentHost.replaceChildren(errorState({ title: 'Impossibile caricare la categoria', message: e.message, onRetry: () => reloadItems() }));
      } else {
        showSnackbar(e.message || 'Errore di caricamento.', { error: true });
      }
    }
  }

  renderControls();
  reloadItems();

  return () => {
    alive = false;
    loadSeq++;
    if (io) io.disconnect();
    unsubSel();
  };
}
