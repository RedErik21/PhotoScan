// Revisione dei duplicati: gruppi di foto identiche o quasi uguali, con la scelta dell'originale da tenere.
// PhotoScan non cancella mai nulla: scegliere un originale riassegna solo i ruoli nel piano (cartelle _Duplicati).
import { api, ApiError, fileUrl } from '../api.js';
import { h, icon } from '../ui/el.js';
import { showSnackbar } from '../ui/snackbar.js';
import { skeletonCard } from '../ui/skeleton.js';
import { emptyState, errorState, loadingBlock } from '../ui/empty-state.js';
import { filterChips } from '../ui/chips.js';
import { confirmDialog } from '../ui/dialog.js';
import { formatBytes, formatDateShort, formatNumber, plural } from '../ui/format.js';

const PAGE = 20;
const TYPES = { exact: 'Identici', similar: 'Quasi uguali' };
const DENSITY_KEY = 'fotoscan.dup.density';
const DENSITIES = [
  { id: 'focus',   label: '1 per volta', icon: 'view_carousel' },
  { id: 'compact', label: 'Compatta',    icon: 'view_comfy' },
  { id: 'medium',  label: 'Normale',     icon: 'grid_view' },
  { id: 'large',   label: 'Grande',      icon: 'view_agenda' },
];
const KIND = { exact: 1, similar: 2 }; // is_dup dei membri mostrati in ciascuna scheda
const REASON = { hash: 'Stesso file', nome: 'Versione modificata', immagine: 'Immagini molto simili' };
const CSS_HREF = 'css/view-duplicates.css';

const isAbort = (e) => e?.name === 'AbortError';
const errMsg = (e) => (e instanceof ApiError && e.message) || 'Operazione non riuscita. Riprova.';

function loadCss() {
  if (document.querySelector(`link[href="${CSS_HREF}"]`)) return;
  const link = document.createElement('link');
  link.rel = 'stylesheet';
  link.href = CSS_HREF;
  document.head.append(link);
}

/** '…\2020\06 - Evento' : ultime due cartelle del percorso (il nome file e' gia' mostrato). */
function abbrevPath(p) {
  if (!p) return '—';
  const dirs = String(p).split(/[\\/]/).filter(Boolean).slice(0, -1);
  if (!dirs.length) return '—';
  return (dirs.length > 2 ? '…\\' : '') + dirs.slice(-2).join('\\');
}

/** Nomi lunghi (es. hash): taglia al centro, cosi' restano visibili l'inizio e l'estensione. Il nome intero va in title. */
function middleEllipsis(name, max = 24) {
  const s = String(name || '');
  if (s.length <= max) return s;
  const tail = Math.min(12, Math.floor(max / 2)); // comprende l'estensione
  const head = max - tail - 1;
  return `${s.slice(0, head)}…${s.slice(-tail)}`;
}

/** Motivo mostrato sotto un duplicato (non l'originale). La percentuale e' quella della singola foto. */
function motivo(f, group) {
  if (f.is_dup === 1) return 'stesso file';
  if (group.reason === 'nome') return 'versione modificata';
  return f.similarity != null ? `molto simile ${Math.round(f.similarity * 100)}%` : 'molto simile';
}

/** Dati del gruppo dopo aver scelto `newId` come originale (aggiornamento ottimistico; la % si aggiorna al ricaricamento). */
function swapMaster(data, newId) {
  const chosen = data.members.find((m) => m.id === newId);
  if (!chosen) return null;
  const kind = chosen.is_dup === 2 ? 2 : 1;
  const members = data.members.filter((m) => m.id !== newId)
    .map((m) => ({ ...m, dup_of: newId, similarity: m.is_dup === 1 ? 1 : null }));
  members.push({ ...data.master, is_dup: kind, dup_of: newId, similarity: kind === 1 ? 1 : null });
  return { ...data, master: { ...chosen, is_dup: 0, dup_of: null, similarity: null }, members };
}

const largest = (data) => [data.master, ...data.members].reduce((a, b) => ((b.size || 0) > (a.size || 0) ? b : a));

function metaList(f) {
  return h('dl', { class: 'dup-meta' },
    h('dt', {}, 'Risoluzione'), h('dd', {}, f.width && f.height ? `${f.width} × ${f.height} px` : '—'),
    h('dt', {}, 'Peso'), h('dd', {}, formatBytes(f.size)),
    h('dt', {}, 'Data'), h('dd', {}, formatDateShort(f.date)),
    h('dt', {}, 'Percorso'), h('dd', { title: f.path || '' }, abbrevPath(f.path)));
}

function metaInline(f) {
  const parts = [];
  if (f.width && f.height) parts.push(`${f.width}×${f.height}`);
  if (f.size != null) parts.push(formatBytes(f.size));
  if (f.date) parts.push(formatDateShort(f.date));
  return h('p', { class: 'dup-meta-inline', title: f.path || '' }, parts.join(' · ') || '—');
}

const badgeOriginale = () => h('span', { class: 'dup-badge' }, icon('check'), 'Originale');
const badgeTrash = () => h('span', { class: 'dup-badge dup-badge--trash' }, icon('delete'), 'Selezionata');

const stat = (value, label) => h('div', { class: 'card stat-card' },
  h('span', { class: 'stat-card__value' }, value),
  h('span', { class: 'stat-card__label' }, label));

/** Scelta Identici / Quasi uguali: segmented button M3 se disponibile nel bundle, altrimenti chip a selezione singola. */
function typeControl(opts, selected, onChange) {
  if (customElements.get('md-outlined-segmented-button-set')) {
    const set = document.createElement('md-outlined-segmented-button-set');
    set.setAttribute('aria-label', 'Tipo di duplicati');
    for (const o of opts) {
      const btn = document.createElement('md-outlined-segmented-button');
      btn.label = o.label;
      btn.selected = o.id === selected;
      btn.addEventListener('click', () => onChange(o.id));
      set.append(btn);
    }
    return set;
  }
  return filterChips(opts, { selected, label: 'Tipo di duplicati', onChange });
}

export async function mount(root, { query = {}, signal } = {}) {
  loadCss();
  let savedDensity = query.view === 'focus' ? 'focus' : 'medium';
  if (!query.view) {
    try {
      const d = localStorage.getItem(DENSITY_KEY);
      if (DENSITIES.some((x) => x.id === d)) savedDensity = d;
    } catch { /* ignora */ }
  }

  const state = {
    alive: true,
    type: query.type === 'similar' ? 'similar' : 'exact',
    density: savedDensity,
    focusIdx: 0,
    summary: null,
    canKeep: true,
    reviewed: false,
    groups: [], // { data, el, busy }
    selected: new Set(), // id delle foto selezionate dall'utente (per Cestino o Conserva)
    fileById: new Map(), // id -> { file, entry }
    offset: 0, total: 0, loaded: false,
    failed: false, // ultima pagina non caricata: niente caricamento automatico finche' l'utente non riprova
    gen: 0, pending: null,
    focus: -1,
    overlay: null,
    bulkBusy: false,
  };

  // Membri mostrati nella scheda corrente: quelli del tipo scelto (il conteggio coincide con il riepilogo).
  const shownMembers = (data) => data.members.filter((m) => m.is_dup === KIND[state.type]);

  // ---------- struttura compatta (nessuna card statistica doppia, foto subito visibili) ----------
  const typeBar = h('div', { class: 'dup-typebar' });
  const summaryInline = h('span', { class: 'dup-summary-inline muted md-typescale-body-small', 'aria-live': 'polite' });
  const listEl = h('div', { class: 'dup-list' });
  const sentinel = h('div', { class: 'dup-sentinel', 'aria-hidden': 'true' });
  const endNote = h('p', { class: 'muted dup-end', hidden: true });
  const skipNote = h('p', { class: 'dup-skip-note', hidden: true },
    "Con l'opzione «salta i duplicati» l'originale non si può cambiare.");

  const reviewBox = h('div', { class: 'dup-review', 'aria-live': 'polite' });
  const bulkBtn = h('md-text-button', { class: 'dup-bulk', onclick: () => keepLargestAll() },
    icon('done_all', { slot: 'icon' }), 'Tieni la migliore in tutti');
  const bulkWrap = h('div', { class: 'dup-bulk-wrap' }, bulkBtn);
  bulkWrap.hidden = true;

  // Barra azioni sticky: azioni rapide globali oppure azioni sulle foto selezionate
  const actionInfo = h('div', { class: 'dup-action-bar__info' });
  const actionButtons = h('div', { class: 'dup-action-bar__actions' });
  const actionBar = h('div', { class: 'dup-action-bar', role: 'region', 'aria-label': 'Azioni sui duplicati' },
    actionInfo, actionButtons);

  const densityBar = h('div', { class: 'dup-density', role: 'group', 'aria-label': 'Dimensione gruppi' });
  const header = h('header', { class: 'view-header view-header--sticky dup-controls' },
    h('div', {}, h('h1', { class: 't-headline' }, 'Duplicati')),
    h('div', { class: 'view-header-actions dup-controls__row' },
      h('div', { class: 'dup-controls__left' }, typeBar, summaryInline),
      h('div', { class: 'dup-controls__right' }, bulkWrap, densityBar)));

  const focusStage = h('section', { class: 'dup-focus-stage', hidden: true, 'aria-live': 'polite' });
  const viewEl = h('div', { class: 'view dup-view', 'data-density': state.density },
    header,
    skipNote,
    actionBar,
    focusStage,
    listEl,
    sentinel,
    endNote);
  root.append(viewEl);

  function setDensity(d) {
    if (!DENSITIES.some((x) => x.id === d)) return;
    state.density = d;
    viewEl.dataset.density = d;
    try { localStorage.setItem(DENSITY_KEY, d); } catch { /* ignora */ }
    renderDensityBar();
    renderFocusStage();
  }

  async function trashGroupCopies(entry) {
    if (state.bulkBusy || !entry) return;
    const copies = shownMembers(entry.data);
    if (!copies.length) return;
    const ids = copies.map((m) => m.id);
    state.bulkBusy = true;
    renderFocusStage();
    try {
      const r = await api.post('/cleanup/trash', { file_ids: ids, confirm: true });
      if (!state.alive) return;
      const failedIds = new Set((r.errors || []).map((e) => e.id));
      const trashedSet = new Set(ids.filter((id) => !failedIds.has(id)));
      pruneFilesInPlace(trashedSet, { promoteSurvivor: true });
      if (r.trashed) {
        showSnackbar(`${plural(r.trashed, 'copia spostata', 'copie spostate')} nel Cestino (${formatBytes(r.freed_bytes)}).`);
        window.dispatchEvent(new CustomEvent('fotoscan:cleanup-changed'));
      }
      refreshSummary();
    } catch (e) {
      if (!isAbort(e) && state.alive) showSnackbar(errMsg(e), { error: true });
    } finally {
      state.bulkBusy = false;
      if (state.alive) renderFocusStage();
    }
  }

  function renderFocusStage() {
    const isFocus = state.density === 'focus';
    focusStage.hidden = !isFocus;
    listEl.hidden = isFocus;
    sentinel.hidden = isFocus;
    endNote.hidden = isFocus || !(state.loaded && state.total > 0 && state.offset >= state.total);
    if (!isFocus) return;

    if (!state.loaded) {
      focusStage.replaceChildren(skeletonCard());
      return;
    }
    if (!state.groups.length) {
      focusStage.replaceChildren(emptyState({
        icon: 'check_circle',
        title: state.type === 'exact' ? 'Nessun duplicato, ottimo!' : 'Nessuna foto quasi uguale, ottimo!',
        message: 'Hai completato tutti i gruppi in questa categoria.',
      }));
      return;
    }
    if (state.focusIdx >= state.groups.length) state.focusIdx = Math.max(0, state.groups.length - 1);
    if (state.focusIdx >= state.groups.length - 3 && hasMore() && !state.pending) loadPage();

    const entry = state.groups[state.focusIdx];
    const master = entry.data.master;
    const copies = shownMembers(entry.data);
    const allPhotos = [master, ...copies];
    const copyBytes = copies.reduce((acc, m) => acc + Number(m.size || 0), 0);
    const copyIds = copies.map((m) => m.id);

    const cards = allPhotos.map((f) => {
      const isMaster = f.id === master.id;
      const img = h('img', {
        class: 'dup-focus-card__img',
        src: fileUrl.thumb(f.id, 512),
        alt: f.name || '',
        loading: 'eager',
        decoding: 'async',
      });
      return h('div', {
        class: `card dup-focus-card${isMaster ? ' is-master' : ''}`,
        onclick: () => {
          if (!isMaster && state.canKeep) commit(entry, f.id).then(() => renderFocusStage());
        },
        title: isMaster ? 'Foto originale che verrà tenuta' : 'Clicca per scegliere questa come originale da tenere',
      },
        h('div', { class: 'dup-focus-card__media' },
          img,
          h('span', { class: `dup-badge${isMaster ? '' : ' dup-badge--trash'}` },
            icon(isMaster ? 'check_circle' : 'delete'),
            isMaster ? 'Tieni (Originale)' : 'Copia da eliminare')),
        h('div', { class: 'dup-focus-card__meta' },
          h('strong', { class: 'md-typescale-title-small', title: f.name || '' }, middleEllipsis(f.name, 28)),
          metaInline(f),
          h('span', { class: 'muted md-typescale-body-small', title: f.path || '' }, abbrevPath(f.path))));
    });

    const head = h('div', { class: 'dup-focus-head' },
      h('span', { class: 'md-typescale-title-small' },
        `Gruppo ${formatNumber(state.focusIdx + 1)} di ${formatNumber(Math.max(state.total, state.groups.length))}`),
      h('span', { class: 'muted md-typescale-body-small' },
        'Clicca su una foto per sceglierla come originale, poi elimina le copie o tienile tutte.'));

    const controls = h('div', { class: 'dup-focus-actions' },
      h('md-outlined-button', {
        disabled: state.focusIdx <= 0 || state.bulkBusy,
        onclick: () => { state.focusIdx = Math.max(0, state.focusIdx - 1); renderFocusStage(); },
      }, icon('arrow_back', { slot: 'icon' }), 'Precedente'),
      h('md-outlined-button', {
        disabled: state.bulkBusy,
        onclick: async () => {
          await keepMany(copyIds, 'Tutte le foto del gruppo conservate.');
          renderFocusStage();
        },
      }, icon('check_circle', { slot: 'icon' }), 'Tieni tutte'),
      h('md-filled-button', {
        class: 'dup-btn-danger',
        disabled: state.bulkBusy || !copyIds.length,
        onclick: () => trashGroupCopies(entry),
      }, icon('delete', { slot: 'icon' }), `Elimina ${plural(copyIds.length, 'copia', 'copie')} (${formatBytes(copyBytes)})`),
      h('md-text-button', {
        disabled: state.focusIdx >= Math.max(state.total, state.groups.length) - 1 || state.bulkBusy,
        onclick: () => { state.focusIdx++; renderFocusStage(); },
      }, 'Salta', icon('arrow_forward', { slot: 'icon' })));

    focusStage.replaceChildren(head, h('div', { class: 'dup-focus-grid' }, ...cards), controls);
  }

  function renderDensityBar() {
    densityBar.replaceChildren(...DENSITIES.map((d) => h('button', {
      type: 'button',
      class: `dup-density__btn${state.density === d.id ? ' is-active' : ''}`,
      'aria-pressed': String(state.density === d.id),
      title: `Vista ${d.label.toLowerCase()}`,
      onclick: () => setDensity(d.id),
    }, icon(d.icon), h('span', {}, d.label))));
  }

  // ---------- riepilogo e tipo ----------
  function renderTypeBar() {
    const opts = Object.entries(TYPES).map(([id, label]) => {
      const n = state.summary?.[id]?.groups;
      return { id, label: n == null ? label : `${label} (${formatNumber(n)})` };
    });
    typeBar.replaceChildren(typeControl(opts, state.type, setType));
  }

  function renderSummary() {
    if (!state.summary) {
      summaryInline.textContent = '';
      return;
    }
    const s = state.summary[state.type];
    summaryInline.textContent = s.files
      ? `${plural(s.files, 'copia', 'copie')} · ${formatBytes(s.bytes)}`
      : 'Nessuna copia';
    renderActionBar();
  }

  function registerGroupFiles(entry) {
    state.fileById.set(entry.data.master.id, { file: entry.data.master, entry });
    for (const m of shownMembers(entry.data)) {
      state.fileById.set(m.id, { file: m, entry });
    }
  }

  function allVisibleCopyIds() {
    const out = [];
    for (const entry of state.groups) {
      for (const m of shownMembers(entry.data)) out.push(m.id);
    }
    return out;
  }

  function countFullSelectedGroups() {
    let n = 0;
    for (const entry of state.groups) {
      const ids = [entry.data.master.id, ...shownMembers(entry.data).map((m) => m.id)];
      if (ids.length > 0 && ids.every((id) => state.selected.has(id))) n++;
    }
    return n;
  }

  function renderActionBar() {
    const selIds = [...state.selected];
    const hasSel = selIds.length > 0;
    const sum = state.summary?.[state.type];
    const hasGroups = (sum?.files || 0) > 0 || state.groups.length > 0;
    actionBar.hidden = !hasGroups && !hasSel;
    actionBar.classList.toggle('has-selection', hasSel);

    if (hasSel) {
      let bytes = 0;
      const touchedEntries = new Set();
      for (const id of selIds) {
        const ref = state.fileById.get(id);
        bytes += Number(ref?.file?.size || 0);
        if (ref?.entry) touchedEntries.add(ref.entry);
      }
      const fullGroups = countFullSelectedGroups();
      // Se e' selezionata 1 sola foto non-master, offri "Imposta come originale"
      const singleRef = selIds.length === 1 ? state.fileById.get(selIds[0]) : null;
      const canSetMaster = !!(state.canKeep && singleRef && singleRef.entry.data.master.id !== singleRef.file.id);
      // Se tutte le foto selezionate appartengono a 1 gruppo (o piu' gruppi), "Conserva interi gruppi" tiene tutte le copie di quei gruppi
      const groupCopyIds = [];
      for (const entry of touchedEntries) {
        for (const m of shownMembers(entry.data)) groupCopyIds.push(m.id);
      }
      const showKeepWholeGroups = groupCopyIds.length > 0 && groupCopyIds.some((id) => !state.selected.has(id));

      actionInfo.replaceChildren(...[
        h('strong', {}, `${plural(selIds.length, 'foto selezionata', 'foto selezionate')} · ${formatBytes(bytes)}`),
        fullGroups > 0
          ? h('span', { class: 'dup-action-bar__warn' },
            icon('warning'),
            `${plural(fullGroups, 'gruppo selezionato per intero', 'gruppi selezionati per intero')}`)
          : null,
      ].filter(Boolean));
      actionButtons.replaceChildren(
        h('md-text-button', { disabled: state.bulkBusy, onclick: () => clearSelection() }, 'Deseleziona'),
        canSetMaster
          ? h('md-outlined-button', {
            disabled: state.bulkBusy,
            onclick: async () => {
              await commit(singleRef.entry, singleRef.file.id);
              clearSelection();
            },
          }, icon('bookmark_added', { slot: 'icon' }), 'Imposta come originale')
          : null,
        h('md-outlined-button', {
          disabled: state.bulkBusy,
          title: 'Marca le foto selezionate come foto normali da conservare (escono dai duplicati)',
          onclick: () => keepMany(selIds),
        }, icon('check_circle', { slot: 'icon' }), 'Non sono duplicati'),
        h('md-filled-button', {
          class: 'dup-btn-danger',
          disabled: state.bulkBusy,
          onclick: () => trashSelected(),
        }, icon('delete', { slot: 'icon' }), `Cestino (${formatBytes(bytes)})`));
      return;
    }

    const visibleCopies = allVisibleCopyIds();
    const totalFiles = sum?.files || 0;
    const totalBytes = sum?.bytes || 0;
    const typeWord = state.type === 'exact' ? 'identiche' : 'quasi uguali';

    actionInfo.replaceChildren(
      h('span', { class: 'muted' }, 'Spunta le foto da spostare nel Cestino, oppure usa un’azione rapida:'));
    actionButtons.replaceChildren(
      visibleCopies.length
        ? h('md-text-button', {
          disabled: state.bulkBusy,
          onclick: () => selectVisibleCopies(),
        }, icon('checklist', { slot: 'icon' }), `Seleziona copie visibili (${formatNumber(visibleCopies.length)})`)
        : null,
      totalFiles > 0
        ? h('md-filled-button', {
          class: 'dup-btn-danger',
          disabled: state.bulkBusy,
          onclick: () => trashAllCopies(),
        }, icon('delete_sweep', { slot: 'icon' }), `Sposta tutte nel Cestino (${formatBytes(totalBytes)})`)
        : null);
  }

  function syncItemSelectionDom(id) {
    const on = state.selected.has(id);
    const el = listEl.querySelector(`.dup-item[data-id="${id}"]`);
    if (!el) return;
    el.classList.toggle('is-selected', on);
    const btn = el.querySelector('.dup-item__sel');
    if (btn) btn.setAttribute('aria-pressed', String(on));
    const statusSlot = el.querySelector('.dup-item__status');
    if (statusSlot) {
      const isMaster = el.dataset.master === '1';
      statusSlot.replaceChildren(on ? badgeTrash() : (isMaster ? badgeOriginale() : ''));
    }
  }

  function toggleSelect(id) {
    if (state.selected.has(id)) state.selected.delete(id);
    else state.selected.add(id);
    syncItemSelectionDom(id);
    renderActionBar();
  }

  function selectVisibleCopies() {
    for (const id of allVisibleCopyIds()) {
      state.selected.add(id);
      syncItemSelectionDom(id);
    }
    renderActionBar();
  }

  function clearSelection() {
    const prev = [...state.selected];
    state.selected.clear();
    for (const id of prev) syncItemSelectionDom(id);
    renderActionBar();
  }

  /** Aggiorna in place i gruppi dopo aver conservato o eliminato alcuni file, preservando selezione e scroll. */
  function pruneFilesInPlace(removedSet, { promoteSurvivor = false } = {}) {
    for (const id of removedSet) {
      state.selected.delete(id);
      state.fileById.delete(id);
    }
    const keptGroups = [];
    let removedGroups = 0;
    for (const entry of state.groups) {
      const data = entry.data;
      const masterGone = promoteSurvivor && removedSet.has(data.master.id);
      const remMembers = data.members.filter((m) => !removedSet.has(m.id));
      const touched = masterGone || remMembers.length !== data.members.length;
      if (!touched) {
        keptGroups.push(entry);
        continue;
      }
      let nextData = null;
      if (masterGone) {
        if (remMembers.length >= 2) {
          const [newMaster, ...rest] = remMembers;
          nextData = {
            ...data,
            master: { ...newMaster, is_dup: 0, dup_of: null, similarity: null },
            members: rest.map((m) => ({ ...m, dup_of: newMaster.id })),
          };
        }
      } else {
        nextData = { ...data, members: remMembers };
      }
      if (!nextData || shownMembers(nextData).length === 0) {
        // Rimuovi dalla selezione eventuali superstiti del gruppo che esce dall'elenco
        state.selected.delete(data.master.id);
        state.fileById.delete(data.master.id);
        for (const m of data.members) {
          state.selected.delete(m.id);
          state.fileById.delete(m.id);
        }
        entry.el.remove();
        removedGroups++;
      } else {
        entry.data = nextData;
        registerGroupFiles(entry);
        keptGroups.push(entry);
      }
    }
    state.groups = keptGroups;
    if (removedGroups > 0) {
      state.total = Math.max(0, state.total - removedGroups);
      state.offset = Math.max(0, state.offset - removedGroups);
    }
    // Ridisegna solo i gruppi rimasti che sono stati modificati
    for (const entry of state.groups) {
      const renderedCount = entry.el.querySelectorAll('.dup-item').length;
      const expectedCount = 1 + shownMembers(entry.data).length;
      if (renderedCount !== expectedCount || Number(entry.el.querySelector('.dup-item')?.dataset.id) !== entry.data.master.id) {
        setGroupData(entry, entry.data);
      }
    }
    if (!state.groups.length && !hasMore()) showEmpty();
    updateEnd();
    renderActionBar();
    maybeLoad();
  }

  /** Segna un elenco di file come normali foto da conservare (anche piu' di una per gruppo), senza perdere le selezioni sugli altri gruppi. */
  async function keepMany(ids, label = null) {
    const uniq = [...new Set(ids)];
    if (!uniq.length || state.bulkBusy) return;
    state.bulkBusy = true;
    renderActionBar();
    try {
      const r = await api.post('/duplicates/keep-many', { file_ids: uniq });
      if (!state.alive) return;
      pruneFilesInPlace(new Set(uniq));
      showSnackbar(label || (r.kept
        ? `${plural(r.kept, 'foto conservata', 'foto conservate')} come normale.`
        : 'Le foto selezionate erano già conservate come originali.'));
      window.dispatchEvent(new CustomEvent('fotoscan:cleanup-changed'));
      refreshSummary();
    } catch (e) {
      if (!isAbort(e) && state.alive) showSnackbar(errMsg(e), { error: true });
    } finally {
      state.bulkBusy = false;
      if (state.alive) renderActionBar();
    }
  }

  /** Sposta nel Cestino tutte le foto selezionate dall'utente across tutti i gruppi, mantenendo la posizione di scroll. */
  async function trashSelected() {
    const ids = [...state.selected];
    if (!ids.length || state.bulkBusy) return;
    let bytes = 0;
    for (const id of ids) bytes += Number(state.fileById.get(id)?.file?.size || 0);
    const fullGroups = countFullSelectedGroups();
    const warn = fullGroups > 0
      ? ` Attenzione: in ${plural(fullGroups, 'gruppo', 'gruppi')} hai selezionato tutte le foto (non resterà alcuna copia).`
      : '';
    const ok = await confirmDialog({
      title: `Spostare ${plural(ids.length, 'foto selezionata', 'foto selezionate')} nel Cestino?`,
      message: `Libererai circa ${formatBytes(bytes)}. Potrai sempre recuperarle dal Cestino di Windows.${warn}`,
      confirmLabel: 'Sposta nel Cestino',
      danger: true,
    });
    if (!ok || !state.alive) return;
    state.bulkBusy = true;
    renderActionBar();
    try {
      const r = await api.post('/cleanup/trash', { file_ids: ids, confirm: true });
      if (!state.alive) return;
      const failedIds = new Set((r.errors || []).map((e) => e.id));
      const trashedSet = new Set(ids.filter((id) => !failedIds.has(id)));
      pruneFilesInPlace(trashedSet, { promoteSurvivor: true });
      if (r.trashed) {
        showSnackbar(`${plural(r.trashed, 'foto spostata', 'foto spostate')} nel Cestino (${formatBytes(r.freed_bytes)} liberati).`);
        window.dispatchEvent(new CustomEvent('fotoscan:cleanup-changed'));
      }
      if (r.errors?.length) showSnackbar(`${plural(r.errors.length, 'file non spostato', 'file non spostati')}.`, { error: true });
      refreshSummary();
    } catch (e) {
      if (!isAbort(e) && state.alive) showSnackbar(errMsg(e), { error: true });
    } finally {
      state.bulkBusy = false;
      if (state.alive) renderActionBar();
    }
  }

  /** 1 clic: elimina tutte le copie extra del tab corrente su tutto l'archivio, tenendo tutti gli originali. */
  async function trashAllCopies() {
    if (state.bulkBusy) return;
    let plan;
    try {
      plan = await api.post('/duplicates/trash-copies', { type: state.type, dry_run: true });
    } catch (e) {
      if (!isAbort(e) && state.alive) showSnackbar(errMsg(e), { error: true });
      return;
    }
    if (!state.alive) return;
    if (!plan.files) {
      showSnackbar('Non ci sono copie da eliminare in questa scheda.');
      return;
    }
    const typeWord = state.type === 'exact' ? 'identiche' : 'quasi uguali';
    const ok = await confirmDialog({
      title: `Eliminare tutte le ${formatNumber(plan.files)} copie ${typeWord}?`,
      message: `Verranno spostate nel Cestino di Windows le copie di ${plural(plan.groups, 'gruppo', 'gruppi')}, liberando circa ${formatBytes(plan.bytes)}. Tutti gli originali restano al sicuro e potrai sempre recuperare le copie dal Cestino.`,
      confirmLabel: `Sposta ${formatNumber(plan.files)} copie nel Cestino`,
      danger: true,
    });
    if (!ok || !state.alive) return;
    state.bulkBusy = true;
    renderActionBar();
    try {
      const r = await api.post('/duplicates/trash-copies', { type: state.type, dry_run: false, confirm: true });
      if (!state.alive) return;
      state.selected.clear();
      if (r.trashed) {
        showSnackbar(`${plural(r.trashed, 'copia spostata', 'copie spostate')} nel Cestino (${formatBytes(r.freed_bytes)} liberati).`);
        window.dispatchEvent(new CustomEvent('fotoscan:cleanup-changed'));
      }
      if (r.errors?.length) showSnackbar(`${plural(r.errors.length, 'copia non spostata', 'copie non spostate')}.`, { error: true });
      reset();
      refreshSummary();
    } catch (e) {
      if (!isAbort(e) && state.alive) showSnackbar(errMsg(e), { error: true });
    } finally {
      state.bulkBusy = false;
      if (state.alive) renderActionBar();
    }
  }

  /** Passo 2 completato: scelta dell'utente (si puo' riaprire). */
  function renderReview() {
    if (!state.summary) { reviewBox.replaceChildren(); return; }
    if (state.reviewed) {
      reviewBox.replaceChildren(
        h('p', { class: 'dup-review__done' }, icon('task_alt'), 'Controllo completato'),
        h('md-text-button', { onclick: () => setReviewed(false) }, 'Riapri il controllo'));
    } else {
      reviewBox.replaceChildren(
        h('p', { class: 'muted dup-review__hint' }, 'Quando hai scelto gli originali, passa al passo successivo.'),
        h('md-filled-tonal-button', { onclick: () => setReviewed(true) },
          icon('task_alt', { slot: 'icon' }), 'Ho finito di controllare'));
    }
  }

  async function setReviewed(value) {
    try {
      await api.post('/duplicates/reviewed', { value });
    } catch (e) {
      if (!isAbort(e) && state.alive) showSnackbar(errMsg(e), { error: true });
      return;
    }
    if (!state.alive) return;
    state.reviewed = value;
    renderReview();
    window.dispatchEvent(new CustomEvent('fotoscan:dup-reviewed', { detail: { reviewed: value } }));
    if (value) showSnackbar('Controllo duplicati completato.');
  }

  async function refreshSummary() {
    try {
      state.summary = await api.get('/duplicates/summary', null, signal);
      if (!state.alive) return;
      state.reviewed = state.summary.reviewed === true;
      renderTypeBar();
      renderSummary();
      renderReview();
    } catch (e) {
      if (!isAbort(e) && state.alive) showSnackbar(errMsg(e), { error: true });
    }
  }

  async function loadSettings() {
    try {
      const s = await api.get('/settings', null, signal);
      state.canKeep = s?.dup_mode !== 'skip';
    } catch (e) {
      if (!isAbort(e)) showSnackbar(errMsg(e), { error: true });
    }
    skipNote.hidden = state.canKeep;
    bulkWrap.hidden = !state.canKeep;
  }

  // ---------- elenco gruppi ----------
  function hasMore() {
    return !state.loaded || state.offset < state.total;
  }

  function updateEnd() {
    const done = state.loaded && state.total > 0 && state.offset >= state.total;
    endNote.hidden = !done;
    endNote.textContent = done ? `Hai visto tutti i ${formatNumber(state.total)} gruppi.` : '';
  }

  function showEmpty() {
    listEl.replaceChildren(emptyState({
      icon: 'check_circle',
      title: state.type === 'exact' ? 'Nessun duplicato, ottimo!' : 'Nessuna foto quasi uguale, ottimo!',
      message: 'Non ci sono copie ripetute in questa categoria.',
    }));
  }

  function retryBlock(message) {
    return h('div', { class: 'dup-retry' },
      h('p', { class: 'muted' }, message),
      h('md-text-button', { onclick: () => { state.failed = false; loadPage(); } },
        icon('refresh', { slot: 'icon' }), 'Riprova'));
  }

  function reset() {
    state.gen++;
    state.pending = null;
    state.groups = [];
    state.selected.clear();
    state.fileById.clear();
    state.offset = 0;
    state.total = 0;
    state.loaded = false;
    state.failed = false;
    state.focus = -1;
    state.focusIdx = 0;
    endNote.hidden = true;
    renderActionBar();
    renderFocusStage();
    listEl.replaceChildren(...[0, 1, 2].map(() => {
      const sk = skeletonCard();
      sk.classList.add('dup-skel');
      return sk;
    }));
    loadPage();
  }

  function loadPage() {
    if (state.pending) return state.pending;
    const gen = state.gen;
    const type = state.type;

    async function run() {
      sentinel.replaceChildren(loadingBlock('Carico altri gruppi…'));
      await settingsReady; // i pulsanti dipendono da dup_mode: si disegna solo quando e' noto
      if (gen !== state.gen || !state.alive) return;
      try {
        const data = await api.get('/duplicates', { type, offset: state.offset, limit: PAGE }, signal);
        if (gen !== state.gen || !state.alive) return;
        state.total = data.total;
        state.loaded = true;
        listEl.querySelectorAll('.dup-skel').forEach((n) => n.remove());
        for (const g of data.groups) {
          const entry = { data: g, el: null, busy: false };
          state.groups.push(entry);
          registerGroupFiles(entry);
          entry.el = groupEl(entry);
          listEl.append(entry.el);
        }
        state.offset += data.groups.length;
        if (!state.total) showEmpty();
        updateEnd();
        renderActionBar();
        renderFocusStage();
      } catch (e) {
        if (gen !== state.gen || !state.alive || isAbort(e)) return;
        listEl.querySelectorAll('.dup-skel').forEach((n) => n.remove());
        if (!state.groups.length) {
          listEl.replaceChildren(errorState({ message: errMsg(e), onRetry: reset }));
        } else {
          state.failed = true; // niente nuovo tentativo automatico: evita un ciclo di richieste
          sentinel.replaceChildren(retryBlock(errMsg(e)));
        }
        showSnackbar(errMsg(e), { error: true });
      } finally {
        if (gen === state.gen && !state.failed) sentinel.replaceChildren();
      }
    }

    const p = run().then(() => {
      if (state.pending === p) state.pending = null;
      maybeLoad();
    });
    state.pending = p;
    return p;
  }

  function maybeLoad() {
    if (state.alive && state.loaded && !state.failed && hasMore() && !state.pending) loadPage();
  }

  function setGroupData(entry, data) {
    entry.data = data;
    const el = groupEl(entry);
    const hadFocus = entry.el.contains(document.activeElement);
    entry.el.replaceWith(el);
    entry.el = el;
    if (hadFocus) el.focus({ preventScroll: true });
    if (state.overlay?.getGi() === state.groups.indexOf(entry)) state.overlay.render();
  }

  /** Sceglie `newId` come originale del gruppo `entry`. Ottimistico: ripristina se il server rifiuta. */
  async function commit(entry, newId, isUndo = false) {
    if (!entry) return;
    if (state.groups.indexOf(entry) < 0) {
      showSnackbar('Questo gruppo non è più in elenco: aggiorna la pagina.', { error: true });
      return;
    }
    if (entry.busy || entry.data.master.id === newId) return;
    if (!state.canKeep) {
      showSnackbar("Con «salta i duplicati» l'originale non si può cambiare.", { error: true });
      return;
    }
    const prev = entry.data;
    const next = swapMaster(prev, newId);
    if (!next) return;
    entry.busy = true;
    setGroupData(entry, next);
    try {
      await api.post('/duplicates/keep', { master_id: newId, member_ids: next.members.map((m) => m.id) });
    } catch (e) {
      if (isAbort(e) || !state.alive) return;
      setGroupData(entry, prev);
      showSnackbar(errMsg(e), { error: true });
      if (e.status === 409) reset(); // il gruppo e' cambiato sul server: ricarica l'elenco
      return;
    } finally {
      entry.busy = false;
    }
    if (!state.alive) return;
    if (isUndo) {
      showSnackbar('Scelta annullata.');
    } else {
      showSnackbar('Originale aggiornato.', {
        actionLabel: 'Annulla',
        onAction: () => commit(entry, prev.master.id, true),
      });
    }
    refreshSummary();
  }

  function keepLargest(gi) {
    const entry = state.groups[gi];
    if (!entry) return;
    const big = largest(entry.data);
    if (big.id === entry.data.master.id) {
      showSnackbar('La foto più pesante è già quella tenuta.');
      return;
    }
    commit(entry, big.id);
  }

  /** Tutti i gruppi: la foto piu' pesante diventa l'originale. Prima conta, poi chiede conferma. */
  async function keepLargestAll() {
    if (!state.canKeep || bulkBtn.disabled) return;
    let plan;
    try {
      plan = await api.post('/duplicates/keep-largest', { dry_run: true });
    } catch (e) {
      if (!isAbort(e) && state.alive) showSnackbar(errMsg(e), { error: true });
      return;
    }
    if (!state.alive) return;
    if (!plan.changed) {
      showSnackbar('In tutti i gruppi è già tenuta la foto più pesante.');
      return;
    }
    const ok = await confirmDialog({
      title: 'Tenere la migliore in tutti i gruppi?',
      message: `${formatNumber(plan.changed)} gruppi cambiano originale: quello con il file più pesante (di solito la versione con più dettagli). `
        + 'Nessun file viene eliminato: potrai poi spostare le copie nel Cestino quando vuoi.',
      confirmLabel: 'Sì, tieni le migliori',
    });
    if (!ok || !state.alive) return;
    bulkBtn.disabled = true;
    try {
      const r = await api.post('/duplicates/keep-largest', { dry_run: false });
      if (!state.alive) return;
      showSnackbar(`Fatto: ${formatNumber(r.changed)} gruppi aggiornati. Nessun file cancellato.`);
      reset();
      refreshSummary();
    } catch (e) {
      if (!isAbort(e) && state.alive) showSnackbar(errMsg(e), { error: true });
    } finally {
      bulkBtn.disabled = false;
    }
  }

  function itemEl(entry, f, data, mi, isMaster) {
    const isSel = state.selected.has(f.id);
    const selBtn = h('button', {
      class: 'dup-item__sel', type: 'button',
      'aria-label': `Seleziona ${f.name}`,
      'aria-pressed': String(isSel),
      title: 'Seleziona per Cestino o per conservare',
      onclick: (e) => { e.stopPropagation(); toggleSelect(f.id); },
    }, icon('check'));

    const media = h('button', {
      class: 'dup-item__media', type: 'button',
      'aria-label': `Apri il confronto: ${f.name}`,
      onclick: (e) => {
        if (e.ctrlKey || e.metaKey || e.shiftKey) { e.preventDefault(); toggleSelect(f.id); return; }
        openCompare(state.groups.indexOf(entry), mi);
      },
    }, h('img', { src: fileUrl.thumb(f.id, 512), alt: '', loading: 'lazy', decoding: 'async' }));

    return h('figure', {
      class: `dup-item${isMaster ? ' is-master' : ''}${isSel ? ' is-selected' : ''}`,
      'data-id': String(f.id),
      'data-master': isMaster ? '1' : '0',
    },
      h('div', { class: 'dup-item__media-wrap' }, media, selBtn),
      h('figcaption', {
        class: 'dup-item__body',
        onclick: () => toggleSelect(f.id),
      },
        h('div', { class: 'dup-item__status' }, isSel ? badgeTrash() : (isMaster ? badgeOriginale() : null)),
        h('p', { class: 'dup-item__name', title: f.name }, middleEllipsis(f.name)),
        isMaster ? null : h('p', { class: 'dup-item__reason muted' }, motivo(f, data)),
        metaInline(f),
        metaList(f)));
  }

  function groupEl(entry) {
    const data = entry.data;
    const gi = state.groups.indexOf(entry);
    const shown = shownMembers(data);
    const copyIds = shown.map((m) => m.id);
    const copiesBytes = shown.reduce((s, m) => s + Number(m.size || 0), 0);

    const compareBtn = h('md-text-button', {
      onclick: () => openCompare(state.groups.indexOf(entry), 0),
    }, icon('compare', { slot: 'icon' }), 'Confronta');

    const selCopiesBtn = h('md-text-button', {
      onclick: () => {
        const allOn = copyIds.every((id) => state.selected.has(id));
        for (const id of copyIds) {
          if (allOn) state.selected.delete(id);
          else state.selected.add(id);
          syncItemSelectionDom(id);
        }
        renderActionBar();
      },
    }, icon('checklist', { slot: 'icon' }), 'Seleziona copie');

    const trashGrpBtn = h('md-text-button', {
      class: 'dup-group__trash',
      title: `Sposta ${plural(copyIds.length, 'copia', 'copie')} nel Cestino tenendo l'originale`,
      onclick: () => trashGroupCopies(entry),
    }, icon('delete', { slot: 'icon' }), `Elimina copie (${formatBytes(copiesBytes)})`);

    const card = h('article', { class: 'card dup-group', tabindex: '-1', 'aria-label': `Gruppo ${gi + 1}` },
      h('header', { class: 'dup-group__head' },
        h('div', { class: 'dup-group__heading' },
          h('h2', { class: 'dup-group__title t-title' }, `Gruppo ${gi + 1}`),
          h('p', { class: 'dup-group__sub muted' },
            `${REASON[data.reason] || 'Foto simili'} · ${plural(shown.length, 'copia', 'copie')} · ${formatBytes(copiesBytes)}`)),
        h('div', { class: 'dup-group__actions' }, compareBtn, selCopiesBtn, trashGrpBtn)),
      h('div', { class: 'dup-compare' },
        itemEl(entry, data.master, data, 0, true),
        ...shown.map((m, i) => itemEl(entry, m, data, i, false))));
    card.addEventListener('focusin', () => { state.focus = state.groups.indexOf(entry); });
    return card;
  }

  // ---------- confronto a schermo intero ----------
  function openCompare(gi, mi = 0) {
    if (state.overlay || !state.groups[gi]) return;
    let cur = gi;
    let sel = mi;

    const title = h('div', { class: 'viewer-topbar__title' });
    const closeBtn = h('md-icon-button', { 'aria-label': 'Chiudi il confronto', onclick: () => close() }, icon('close'));
    const prevBtn = h('button', { class: 'viewer-nav viewer-nav--prev', type: 'button', 'aria-label': 'Gruppo precedente', onclick: () => step(-1) }, icon('chevron_left'));
    const nextBtn = h('button', { class: 'viewer-nav viewer-nav--next', type: 'button', 'aria-label': 'Gruppo successivo', onclick: () => step(1) }, icon('chevron_right'));
    const stage = h('div', { class: 'viewer-stage' });
    const bar = h('div', { class: 'dup-compare__members' });
    const overlay = h('div', { class: 'viewer-overlay dup-compare-overlay', role: 'dialog', 'aria-modal': 'true', 'aria-label': 'Confronto tra duplicati' },
      h('div', { class: 'viewer-topbar' }, title, closeBtn),
      stage,
      bar);

    const panel = (f, g, isMaster) => h('figure', { class: 'dup-compare__panel' },
      h('div', { class: 'dup-compare__media' },
        h('img', { class: 'dup-compare__img', src: fileUrl.display(f.id, 2048), alt: f.name, decoding: 'async' })),
      h('figcaption', { class: 'dup-compare__caption' },
        h('div', { class: 'dup-item__status' }, isMaster ? badgeOriginale() : null),
        h('p', { class: 'dup-compare__name', title: f.name }, middleEllipsis(f.name, 44)),
        h('p', { class: 'dup-item__reason' }, isMaster ? 'Va nella cartella principale' : motivo(f, g)),
        metaList(f)));

    function render() {
      const g = state.groups[cur].data;
      const members = shownMembers(g);
      sel = Math.min(sel, Math.max(0, members.length - 1));
      const other = members[sel];
      title.textContent = `${REASON[g.reason] || 'Foto simili'} · gruppo ${cur + 1} di ${formatNumber(state.total)}`;
      prevBtn.disabled = cur === 0;
      nextBtn.disabled = cur >= state.groups.length - 1 && !hasMore();
      stage.replaceChildren(prevBtn, nextBtn,
        h('div', { class: 'dup-compare__grid' },
          panel(g.master, g, true),
          other ? panel(other, g, false) : null));
      // barra in basso: scelta del confronto (se piu' membri) e azione "Tieni questo" sul membro mostrato
      const entry = state.groups[cur];
      const keepNow = other && state.canKeep
        ? h('md-filled-tonal-button', {
          class: 'dup-compare__keep',
          'aria-label': `Tieni questo: ${other.name}`,
          onclick: () => commit(entry, other.id),
        }, icon('bookmark_added', { slot: 'icon' }), 'Tieni questo')
        : null;
      const keepBoth = other
        ? h('md-outlined-button', {
          onclick: () => { close(); keepMany([other.id], 'Entrambe le foto conservate.'); },
        }, icon('library_add_check', { slot: 'icon' }), 'Conserva entrambe')
        : null;
      bar.replaceChildren(
        ...(members.length > 1 ? members.map((m, i) => h('md-outlined-button', {
          class: i === sel ? 'is-current' : '',
          'aria-pressed': String(i === sel),
          onclick: () => { sel = i; render(); },
        }, `Duplicato ${i + 1} di ${members.length}`)) : []),
        keepNow,
        keepBoth);
    }

    async function step(d) {
      const n = cur + d;
      if (n < 0) return;
      if (n >= state.groups.length) {
        if (!hasMore()) return;
        await loadPage();
        if (n >= state.groups.length || state.overlay !== ov) return;
      }
      cur = n;
      sel = 0;
      render();
    }

    function close() {
      if (state.overlay !== ov) return;
      document.removeEventListener('keydown', onKey);
      overlay.remove();
      state.overlay = null;
      state.groups[cur]?.el.focus({ preventScroll: true });
    }

    // Tab resta dentro il confronto (e' una finestra modale)
    function trapTab(e) {
      const items = [...overlay.querySelectorAll('button, md-icon-button, md-outlined-button, md-filled-tonal-button')]
        .filter((el) => !el.disabled);
      if (!items.length) return;
      e.preventDefault();
      const i = items.indexOf(document.activeElement);
      const n = e.shiftKey
        ? (i <= 0 ? items.length - 1 : i - 1)
        : (i < 0 || i >= items.length - 1 ? 0 : i + 1);
      items[n].focus();
    }

    const onKey = (e) => {
      if (e.altKey || e.ctrlKey || e.metaKey) return;
      if (e.key === 'Tab') trapTab(e);
      else if (e.key === 'Escape') { e.preventDefault(); close(); }
      else if (e.key === 'ArrowLeft') { e.preventDefault(); step(-1); }
      else if (e.key === 'ArrowRight') { e.preventDefault(); step(1); }
      else if (e.key === 'k' || e.key === 'K') { e.preventDefault(); keepLargest(cur); }
    };

    const ov = { getGi: () => cur, render, close };
    state.overlay = ov;
    document.addEventListener('keydown', onKey);
    document.body.append(overlay);
    render();
    closeBtn.focus();
  }

  // ---------- navigazione da tastiera ----------
  function focusGroup(n) {
    state.focus = n;
    const el = state.groups[n].el;
    el.scrollIntoView({ block: 'nearest' });
    el.focus({ preventScroll: true });
  }

  async function moveFocus(d) {
    if (!state.groups.length) return;
    let n = Math.max(0, state.focus < 0 ? 0 : state.focus + d);
    if (n >= state.groups.length) {
      if (!hasMore()) return;
      await loadPage();
      if (n >= state.groups.length) return;
    }
    focusGroup(n);
  }

  // Le frecce agiscono solo quando il fuoco e' nell'elenco: altrove la pagina scorre normalmente.
  const onKey = (e) => {
    if (state.overlay || e.defaultPrevented || e.altKey || e.ctrlKey || e.metaKey) return;
    if (e.target?.closest?.('input, textarea, select, [contenteditable="true"], md-chip-set, md-outlined-segmented-button-set, md-menu')) return;
    if (!listEl.contains(document.activeElement)) return;
    if (e.key === 'ArrowDown' || e.key === 'ArrowRight') { e.preventDefault(); moveFocus(1); }
    else if (e.key === 'ArrowUp' || e.key === 'ArrowLeft') { e.preventDefault(); moveFocus(-1); }
    else if ((e.key === 'k' || e.key === 'K') && state.focus >= 0) { e.preventDefault(); keepLargest(state.focus); }
  };

  function setType(t) {
    if (t === state.type) return;
    state.type = t;
    state.overlay?.close();
    history.replaceState(null, '', `#/duplicates?type=${t}`);
    renderTypeBar();
    renderSummary();
    reset();
  }

  // ---------- avvio ----------
  renderTypeBar();
  renderDensityBar();
  renderSummary();
  renderReview();
  const settingsReady = loadSettings();

  const observer = new IntersectionObserver((entries) => {
    if (entries.some((en) => en.isIntersecting)) maybeLoad();
  }, { rootMargin: '600px 0px' });
  observer.observe(sentinel);
  document.addEventListener('keydown', onKey, { signal });

  reset();
  refreshSummary();

  return () => {
    state.alive = false;
    state.gen++;
    state.overlay?.close();
    observer.disconnect();
    document.removeEventListener('keydown', onKey);
  };
}

function decorative(el, extraClass) {
  el.setAttribute('aria-hidden', 'true');
  if (extraClass) el.classList.add(extraClass);
  return el;
}
