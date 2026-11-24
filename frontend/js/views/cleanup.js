// Libera spazio: mostra duplicati, video pesanti, screenshot, documenti/scontrini e foto pesanti,
// indicando quanto spazio si può recuperare e permettendo di spostare i file scelti nel Cestino di Windows.
import { h, icon } from '../ui/el.js';
import { api } from '../api.js';
import { showSnackbar } from '../ui/snackbar.js';
import { confirmDialog } from '../ui/dialog.js';
import { createTile, updateTile } from '../ui/photo-tile.js';
import { createSelection, useRangeSelect } from '../ui/selection.js';
import { skeletonCard, skeletonGrid } from '../ui/skeleton.js';
import { emptyState, errorState } from '../ui/empty-state.js';
import { formatBytes, formatNumber, plural } from '../ui/format.js';

const CATEGORIES = [
  { id: 'duplicates',   icon: 'filter_none',             title: 'Duplicati',             hint: 'Copie identiche o quasi uguali' },
  { id: 'large_videos', icon: 'videocam',                title: 'Video pesanti',         hint: 'Video da almeno 50 MB' },
  { id: 'screenshots',  icon: 'screenshot',              title: 'Screenshot',            hint: 'Schermate salvate nel tempo' },
  { id: 'documents',    icon: 'receipt_long',            title: 'Documenti e scontrini', hint: 'Ricevute, fogli e appunti' },
  { id: 'large_photos', icon: 'photo_size_select_large', title: 'Foto pesanti',          hint: 'Foto da almeno 8 MB' },
];

const PAGE = 60;
const CSS_URL = new URL('../../css/view-cleanup.css', import.meta.url).href;

function loadCss() {
  if (document.querySelector('link[data-view-css="cleanup"]')) return;
  document.head.append(h('link', { rel: 'stylesheet', href: CSS_URL, dataset: { viewCss: 'cleanup' } }));
}

export async function mount(root, ctx) {
  loadCss();
  let alive = true;
  let summary = null;
  let activeCat = CATEGORIES.some((c) => c.id === ctx.query?.cat) ? ctx.query.cat : 'duplicates';
  let cleanupDup = null;
  let dupAc = null;
  let catSeq = 0;

  // Stato della categoria file (non duplicati)
  const selection = createSelection();
  let items = [];
  let total = 0;
  let offset = 0;
  let loadingMore = false;
  let io = null;
  const tileMap = new Map(); // id -> tileEl

  const heroValue = h('p', { class: 'cln-hero__value' }, '—');
  const heroSub = h('p', { class: 'cln-hero__sub' }, 'Analisi dello spazio occupato in corso…');
  const dupSettingsBtn = h('md-outlined-button', { onclick: () => openDupSettingsDialog() },
    icon('tune', { slot: 'icon' }), 'Impostazioni duplicati');
  const hero = h('section', { class: 'card cln-hero' },
    h('div', { class: 'cln-hero__copy' },
      h('p', { class: 'cln-hero__eyebrow' }, 'Spazio recuperabile'),
      heroValue,
      heroSub),
    dupSettingsBtn);

  async function openDupSettingsDialog() {
    let cur;
    try { cur = await api.get('/settings'); } catch (e) { showSnackbar(e.message || 'Impossibile caricare le impostazioni.', { error: true }); return; }
    if (!alive) return;
    const swNear = document.createElement('md-switch');
    swNear.selected = Number(cur.near_dup_threshold || 0) > 0;
    const thrSlider = h('md-slider', {
      min: 0.90, max: 0.999, step: 0.005,
      value: swNear.selected ? Number(cur.near_dup_threshold) : 0.985,
      disabled: !swNear.selected,
    });
    const thrOut = h('strong', { class: 'tabular' }, `${(Number(thrSlider.value) * 100).toFixed(1)}%`);
    thrSlider.addEventListener('input', () => { thrOut.textContent = `${(Number(thrSlider.value) * 100).toFixed(1)}%`; });
    swNear.addEventListener('change', () => { thrSlider.disabled = !swNear.selected; });

    const secField = h('md-outlined-text-field', {
      label: 'Finestra temporale (secondi)', type: 'number', min: 0, max: 86400,
      value: String(cur.near_dup_seconds ?? 120),
    });
    const swName = document.createElement('md-switch');
    swName.selected = !!cur.near_dup_name_rule;

    const dlg = h('md-dialog', {},
      h('div', { slot: 'headline' }, 'Impostazioni duplicati quasi uguali'),
      h('div', { slot: 'content', style: 'display:flex;flex-direction:column;gap:16px;min-width:min(420px,80vw);' },
        h('label', { style: 'display:flex;align-items:center;justify-content:space-between;gap:12px;' },
          h('span', {}, 'Cerca anche foto quasi uguali'), swNear),
        h('div', { style: 'display:flex;flex-direction:column;gap:4px;' },
          h('span', { class: 'muted md-typescale-body-small' }, 'Somiglianza minima: ', thrOut),
          thrSlider),
        secField,
        h('label', { style: 'display:flex;align-items:center;justify-content:space-between;gap:12px;' },
          h('span', {}, 'Riconosci copie «-modificato» dal nome'), swName)),
      h('div', { slot: 'actions' },
        h('md-text-button', { onclick: () => dlg.close('cancel') }, 'Annulla'),
        h('md-filled-button', {
          onclick: async () => {
            const sec = Number(secField.value);
            if (!Number.isFinite(sec) || sec < 0 || sec > 86400) {
              secField.error = true; secField.errorText = 'Valore tra 0 e 86400.'; return;
            }
            try {
              await api.put('/settings', {
                near_dup_threshold: swNear.selected ? Number(Number(thrSlider.value).toFixed(3)) : 0,
                near_dup_seconds: sec,
                near_dup_name_rule: !!swName.selected,
              });
              dlg.close('save');
              showSnackbar('Impostazioni duplicati salvate.');
            } catch (e) {
              showSnackbar(e.message || 'Salvataggio non riuscito.', { error: true });
            }
          },
        }, 'Salva')));
    dlg.addEventListener('closed', () => dlg.remove(), { once: true });
    document.body.append(dlg);
    dlg.show();
  }

  const catRefs = new Map();
  const catsGrid = h('div', { class: 'cln-cats fs-stagger', role: 'tablist', 'aria-label': 'Categorie di pulizia' },
    CATEGORIES.map((c) => {
      const sizeEl = h('span', { class: 'cln-cat__size' }, '—');
      const metaEl = h('span', { class: 'cln-cat__meta' }, c.hint);
      const btn = h('button', {
        type: 'button', role: 'tab', class: 'cln-cat', 'data-id': c.id,
        onclick: () => selectCategory(c.id),
      },
      h('div', { class: 'cln-cat__top' },
        h('span', { class: 'cln-cat__icon', 'aria-hidden': 'true' }, icon(c.icon)),
        sizeEl),
      h('span', { class: 'cln-cat__title' }, c.title),
      metaEl);
      catRefs.set(c.id, { btn, sizeEl, metaEl, cat: c });
      return btn;
    }));

  const panel = h('section', { class: 'cln-panel', role: 'tabpanel' });
  const view = h('div', { class: 'view cln-view' },
    h('header', { class: 'view-header' },
      h('div', {},
        h('h1', { class: 't-headline' }, 'Libera spazio'),
        h('p', { class: 'muted t-body' }, 'Scopri duplicati, video pesanti, screenshot e documenti che occupano spazio. I file eliminati finiscono sempre nel Cestino di Windows.'))),
    hero,
    catsGrid,
    panel);
  root.replaceChildren(view);

  function renderSummary() {
    if (!summary) return;
    heroValue.textContent = summary.total_bytes ? formatBytes(summary.total_bytes) : '0 B';
    heroSub.textContent = summary.total_files
      ? `${plural(summary.total_files, 'file candidato', 'file candidati')} · nessun file viene toccato senza la tua conferma.`
      : 'Ottimo lavoro: non ci sono file pesanti o duplicati evidenti da pulire.';
    for (const c of CATEGORIES) {
      const ref = catRefs.get(c.id);
      const st = summary[c.id] || { count: 0, bytes: 0 };
      ref.sizeEl.textContent = formatBytes(st.bytes || 0);
      ref.metaEl.textContent = c.id === 'duplicates' && st.groups
        ? `${plural(st.count, 'copia', 'copie')} · ${plural(st.groups, 'gruppo', 'gruppi')}`
        : `${plural(st.count, 'file', 'file')} · ${c.hint}`;
    }
  }

  function renderActiveTab() {
    for (const c of CATEGORIES) {
      const ref = catRefs.get(c.id);
      const on = c.id === activeCat;
      ref.btn.classList.toggle('is-active', on);
      ref.btn.setAttribute('aria-selected', String(on));
    }
  }

  function teardownPanel() {
    if (io) { io.disconnect(); io = null; }
    if (dupAc) { dupAc.abort(); dupAc = null; }
    if (cleanupDup) { try { cleanupDup(); } catch (e) { console.error(e); } cleanupDup = null; }
    selection.clear();
    tileMap.clear();
    items = [];
    total = 0;
    offset = 0;
    loadingMore = false;
  }

  async function selectCategory(id) {
    activeCat = id;
    renderActiveTab();
    const my = ++catSeq;
    teardownPanel();

    if (id === 'duplicates') {
      panel.replaceChildren(skeletonCard());
      dupAc = new AbortController();
      try {
        const mod = await import('./duplicates.js');
        if (!alive || my !== catSeq) return;
        panel.replaceChildren();
        const res = await mod.mount(panel, { ...ctx, signal: dupAc.signal });
        if (!alive || my !== catSeq) { if (typeof res === 'function') res(); return; }
        if (typeof res === 'function') cleanupDup = res;
      } catch (e) {
        if (!alive || my !== catSeq) return;
        panel.replaceChildren(errorState({ title: 'Duplicati non disponibili', message: e.message, onRetry: () => selectCategory('duplicates') }));
      }
      return;
    }

    // Categorie di file selezionabili per lo spostamento nel Cestino
    const catInfo = CATEGORIES.find((c) => c.id === id);
    const selInfo = h('span', { class: 'md-typescale-body-medium' }, 'Seleziona i file da spostare nel Cestino');
    const selectAllBtn = h('md-text-button', {
      onclick: () => {
        if (selection.count === items.length && items.length > 0) selection.clear();
        else selection.selectAll(items.map((f) => f.id));
      },
    }, 'Seleziona visibili');
    const trashBtn = h('md-filled-button', {
      class: 'cln-trash-btn', disabled: true,
      onclick: () => trashSelected(),
    }, icon('delete', { slot: 'icon' }), 'Sposta nel Cestino');

    const bar = h('div', { class: 'cln-bar' },
      h('div', { class: 'cln-bar__info' }, selInfo),
      h('div', { class: 'cln-bar__actions' }, selectAllBtn, trashBtn));
    const grid = h('div', { class: 'tile-grid cln-grid' });
    const rangeSelect = useRangeSelect(selection, () => items.map((f) => f.id));
    let lastShift = false;
    grid.addEventListener('click', (e) => { lastShift = e.shiftKey; }, true);
    const moreBtn = h('md-outlined-button', { hidden: true, onclick: () => loadMore(my) }, 'Carica altri');
    const moreWrap = h('div', { class: 'cln-more' }, moreBtn);
    const sentinel = h('div', { style: 'height:1px;' });

    panel.replaceChildren(bar, skeletonGrid(12, { tile: 168 }));

    function syncBar() {
      const ids = [...selection.ids];
      const byId = new Map(items.map((f) => [f.id, f]));
      let bytes = 0;
      for (const fid of ids) bytes += Number(byId.get(fid)?.size || 0);
      if (ids.length) {
        selInfo.textContent = `${plural(ids.length, 'file selezionato', 'file selezionati')} · ${formatBytes(bytes)}`;
        trashBtn.disabled = false;
        trashBtn.replaceChildren(icon('delete', { slot: 'icon' }), `Sposta nel Cestino (${formatBytes(bytes)})`);
      } else {
        selInfo.textContent = `${catInfo.title}: ${plural(total, 'file trovato', 'file trovati')} (ordinati dal più pesante)`;
        trashBtn.disabled = true;
        trashBtn.replaceChildren(icon('delete', { slot: 'icon' }), 'Sposta nel Cestino');
      }
      selectAllBtn.textContent = ids.length && ids.length >= items.length ? 'Deseleziona tutti' : 'Seleziona visibili';
      for (const [fid, el] of tileMap) updateTile(el, { selected: selection.has(fid) });
    }

    const unsubSel = selection.subscribe(syncBar);
    const prevTeardown = cleanupDup;
    cleanupDup = () => { unsubSel(); prevTeardown?.(); };

    async function openViewerAt(idx) {
      try {
        const { openViewer } = await import('./viewer.js');
        openViewer({ items, index: idx });
      } catch {
        showSnackbar('Visualizzatore non disponibile.', { error: true });
      }
    }

    async function trashSelected() {
      const ids = [...selection.ids];
      if (!ids.length) return;
      const byId = new Map(items.map((f) => [f.id, f]));
      let bytes = 0;
      for (const fid of ids) bytes += Number(byId.get(fid)?.size || 0);
      const ok = await confirmDialog({
        title: `Spostare ${plural(ids.length, 'file', 'file')} nel Cestino?`,
        message: `Libererai circa ${formatBytes(bytes)}. I file verranno spostati nel Cestino di Windows (potrai recuperarli in qualsiasi momento) e rimossi dal catalogo.`,
        confirmLabel: 'Sposta nel Cestino',
        danger: true,
      });
      if (!ok || !alive || my !== catSeq) return;
      trashBtn.disabled = true;
      try {
        const r = await api.post('/cleanup/trash', { file_ids: ids, confirm: true });
        if (!alive || my !== catSeq) return;
        const failedIds = new Set((r.errors || []).map((e) => e.id));
        const removed = new Set(ids.filter((fid) => !failedIds.has(fid)));
        items = items.filter((f) => !removed.has(f.id));
        for (const fid of removed) {
          tileMap.get(fid)?.remove();
          tileMap.delete(fid);
        }
        total = Math.max(0, total - removed.size);
        offset = Math.max(0, offset - removed.size);
        selection.clear();
        if (r.trashed) showSnackbar(`${plural(r.trashed, 'file spostato', 'file spostati')} nel Cestino (${formatBytes(r.freed_bytes)} liberati).`);
        if (r.errors?.length) showSnackbar(`${plural(r.errors.length, 'file non spostato', 'file non spostati')}.`, { error: true });
        if (!items.length) {
          panel.replaceChildren(emptyState({ icon: 'check_circle', title: 'Categoria pulita!', message: 'Nessun altro file in questa categoria.' }));
        }
        loadSummary();
      } catch (e) {
        if (alive) showSnackbar(e.message || 'Operazione non riuscita.', { error: true });
      } finally {
        if (alive && my === catSeq) syncBar();
      }
    }

    async function loadMore(seqId) {
      if (!alive || seqId !== catSeq || loadingMore) return;
      if (offset > 0 && items.length >= total) return;
      loadingMore = true;
      moreBtn.disabled = true;
      try {
        const data = await api.get('/cleanup/items', { category: id, limit: PAGE, offset }, ctx.signal);
        if (!alive || seqId !== catSeq) return;
        const batch = data.items || [];
        const isFirst = offset === 0;
        total = data.total || 0;
        const start = items.length;
        items.push(...batch);
        offset += batch.length;

        const nodes = batch.map((f, i) => {
          const tile = createTile(f, {
            size: 168,
            showMeta: true,
            selectable: true,
            selected: selection.has(f.id),
            onOpen: () => (selection.count > 0 || lastShift
              ? rangeSelect(f.id, { shiftKey: lastShift })
              : openViewerAt(start + i)),
            onSelectToggle: (file) => rangeSelect(file.id, { shiftKey: lastShift }),
          });
          tileMap.set(f.id, tile);
          return tile;
        });

        if (isFirst) {
          if (!items.length) {
            panel.replaceChildren(emptyState({
              icon: 'check_circle',
              title: `Nessun elemento in «${catInfo.title}»`,
              message: 'Non ci sono file in questa categoria.',
            }));
            loadingMore = false;
            return;
          }
          grid.replaceChildren(...nodes);
          panel.replaceChildren(bar, grid, moreWrap, sentinel);
          io = new IntersectionObserver((entries) => {
            if (entries.some((e) => e.isIntersecting)) loadMore(seqId);
          }, { rootMargin: '400px 0px' });
          io.observe(sentinel);
        } else {
          grid.append(...nodes);
        }
        moreBtn.hidden = items.length >= total || batch.length === 0;
        moreBtn.disabled = false;
        loadingMore = false;
        syncBar();
      } catch (e) {
        if (!alive || seqId !== catSeq || e?.name === 'AbortError') return;
        loadingMore = false;
        moreBtn.disabled = false;
        if (offset === 0) {
          panel.replaceChildren(errorState({ title: 'Impossibile caricare la categoria', message: e.message, onRetry: () => selectCategory(id) }));
        } else {
          showSnackbar(e.message || 'Errore di caricamento.', { error: true });
        }
      }
    }

    loadMore(my);
  }

  async function loadSummary() {
    try {
      const s = await api.get('/cleanup/summary', undefined, ctx.signal);
      if (!alive) return;
      summary = s;
      renderSummary();
    } catch (e) {
      if (alive && e?.name !== 'AbortError') {
        heroSub.textContent = 'Riavvia PhotoScan per attivare il calcolo dello spazio recuperabile.';
      }
    }
  }

  const onChanged = () => loadSummary();
  window.addEventListener('fotoscan:cleanup-changed', onChanged);

  renderActiveTab();
  await loadSummary();
  if (alive) selectCategory(activeCat);

  return () => {
    alive = false;
    catSeq++;
    window.removeEventListener('fotoscan:cleanup-changed', onChanged);
    teardownPanel();
  };
}
