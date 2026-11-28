// Persone: riconoscimento volti locale, rinomina, unione con annulla, gruppi suggeriti, split volti e nascondi.
// Dati: GET /people, GET /people/suggestion-groups, POST/DELETE /people/not-same,
//       GET /people/{id}/files, PATCH /people/{id}, POST /people/merge, POST /people/merge/{id}/undo,
//       POST /people/split, POST /faces/assign, POST /faces/{id}/exclude, POST /faces/{id}/restore,
//       POST /people/scan, POST /people/recluster, POST /people/download-models.
import { api, fileUrl } from '../api.js';
import { store } from '../store.js';
import { h, icon } from '../ui/el.js';
import { emptyState, errorState } from '../ui/empty-state.js';
import { confirmDialog, promptDialog, choiceDialog } from '../ui/dialog.js';
import { createTile } from '../ui/photo-tile.js';
import { skeletonGrid } from '../ui/skeleton.js';
import { showSnackbar } from '../ui/snackbar.js';
import { plural, formatNumber } from '../ui/format.js';

const FILES_PAGE = 120;

function loadCss() {
  if (document.querySelector('link[data-view-css="people"]')) return;
  const link = document.createElement('link');
  link.rel = 'stylesheet';
  link.href = new URL('../../css/view-people.css', import.meta.url).href;
  link.dataset.viewCss = 'people';
  document.head.append(link);
}

/** Ritaglio del volto con ripiego sulla miniatura della foto e poi icona. */
function faceAvatar(person, size = 256) {
  const alt = person.name || 'Persona senza nome';
  const fallbackIcon = () => h('div', { class: 'person-card__fallback', 'aria-hidden': 'true' }, icon('face'));
  if (!person.cover_face_id && !person.sample_file_id) return fallbackIcon();

  const src = person.cover_face_id
    ? fileUrl.faceCrop(person.cover_face_id, size)
    : fileUrl.thumb(person.sample_file_id, size);
  const img = h('img', { src, alt, loading: 'lazy', decoding: 'async' });
  let triedThumb = !person.cover_face_id;
  img.addEventListener('error', () => {
    if (!triedThumb && person.sample_file_id) {
      triedThumb = true;
      img.src = fileUrl.thumb(person.sample_file_id, size);
    } else {
      img.replaceWith(fallbackIcon());
    }
  });
  return img;
}

// La griglia delle persone si ricostruisce a ogni selezione o ricarica: le immagini gia' decodificate si
// riusano (stesso nodo <img>) invece di ricrearle, cosi' non si riscaricano ne' si ridecodificano.
const avatarCache = new Map(); // chiave persona+copertina+dimensione -> <img> caricata
const AVATAR_CACHE_MAX = 1500;
function cachedFaceAvatar(person, size = 256) {
  const key = `${person.id}:${person.cover_face_id || ''}:${person.sample_file_id || ''}:${size}:${person.name || ''}`;
  const old = avatarCache.get(key);
  if (old && old.complete && old.naturalWidth > 0) return old;
  const node = faceAvatar(person, size);
  if (node.tagName === 'IMG') {
    avatarCache.delete(key);
    avatarCache.set(key, node);
    while (avatarCache.size > AVATAR_CACHE_MAX) avatarCache.delete(avatarCache.keys().next().value);
  }
  return node;
}

/** Somma indicativa delle foto di un insieme di persone (per anteprima prima della conferma). */
function sumPhotos(list) {
  return list.reduce((acc, p) => acc + Number(p?.count || 0), 0);
}

/** Sceglie la destinazione predefinita tra piu' persone senza mostrare dialog (nome > preferita > piu' foto > id minore). */
function defaultMergeTarget(list, preferredTargetId = null) {
  const named = list.filter((p) => p.name && p.name.trim());
  const pool = named.length ? named : list;
  if (preferredTargetId != null) {
    const pref = pool.find((p) => p.id === preferredTargetId);
    if (pref) return pref;
  }
  return [...pool].sort((a, b) => (Number(b.count || 0) - Number(a.count || 0)) || (a.id - b.id))[0];
}

export async function mount(root, { navigate, query = {}, signal } = {}) {
  loadCss();
  const ac = new AbortController();
  const stop = () => ac.abort();
  signal?.addEventListener('abort', stop);
  const sig = ac.signal;

  let disposed = false;
  let people = [];
  let suggestionGroups = [];
  let statusInfo = null;
  let settingsInfo = null;
  let showHidden = false;
  let mergeMode = false;
  const selectedIds = new Set();
  let draggedPersonId = null;
  let activePersonId = query.id && /^\d+$/.test(String(query.id)) ? Number(query.id) : null;
  let detail = null; // { person, items, total, ac, selectMode, selectedFaceIds, ... }
  let lastJobState = store.get('facesJob', null)?.state || null;

  const page = h('div', { class: 'view people-view' });
  const jobSlot = h('div', { hidden: true });
  const contentSlot = h('div', { class: 'people-view' });
  page.append(jobSlot, contentSlot);
  root.replaceChildren(page);

  // ---------- Job SSE cross-pagina (download modelli / scansione / ricalcolo persone) ----------
  async function cancelFaceJob(id) {
    try {
      await api.post(`/jobs/${id}/cancel`, {});
      showSnackbar('Arresto richiesto…');
    } catch (e) {
      showSnackbar(e.message || 'Impossibile fermare l’operazione.', { error: true });
    }
  }

  function renderJob(ev) {
    const prev = lastJobState;
    lastJobState = ev?.state || null;
    if (!ev || ev.state !== 'running') {
      jobSlot.hidden = true;
      jobSlot.replaceChildren();
      if (prev === 'running' && ev) {
        if (ev.state === 'done') {
          showSnackbar(ev.message || 'Operazione volti completata.');
          loadAll();
        } else if (ev.state === 'cancelled') {
          showSnackbar('Analisi volti fermata. Il lavoro svolto finora è stato salvato.');
          loadAll();
        } else if (ev.state === 'error') {
          showSnackbar(ev.error || ev.message || 'Operazione non riuscita.', { error: true });
          loadAll(); // il lavoro svolto prima dell'errore e' salvato
        }
      }
      return;
    }
    const frac = ev.total > 0 ? Math.min(1, Math.max(0, ev.done / ev.total)) : null;
    const bar = h('md-linear-progress', {
      indeterminate: frac === null,
      value: frac ?? 0,
    });
    jobSlot.hidden = false;
    jobSlot.replaceChildren(h('div', { class: 'people-job-banner', role: 'status' },
      h('div', { class: 'people-job-banner__row' },
        h('span', { class: 'md-typescale-title-small' }, ev.message || 'Elaborazione volti in corso…'),
        h('div', { style: 'display:flex;align-items:center;gap:8px;' },
          frac !== null ? h('span', { class: 'muted md-typescale-label-medium' }, `${Math.round(frac * 100)}%`) : null,
          h('md-text-button', { onclick: () => cancelFaceJob(ev.id) }, 'Ferma'))),
      bar));
  }

  const unsubJob = store.subscribe('facesJob', (ev) => { if (!disposed) renderJob(ev); });
  renderJob(store.get('facesJob', null));

  // Protezione dal doppio clic: una sola richiesta di avvio alla volta (il server risponde comunque 409 ai duplicati)
  let startBusy = false;
  async function withStartLock(fn) {
    if (startBusy) return;
    startBusy = true;
    try {
      await fn();
    } finally {
      startBusy = false;
    }
  }

  function startDownloadModels() {
    return withStartLock(async () => {
      try {
        const r = await api.post('/people/download-models', {});
        if (disposed) return;
        if (r.job) store.set('facesJob', r.job);
      } catch (e) {
        if (!disposed) showSnackbar(e.message || 'Impossibile scaricare i modelli.', { error: true });
      }
    });
  }

  function startScanFaces() {
    return withStartLock(async () => {
      try {
        const r = await api.post('/people/scan', {});
        if (disposed) return;
        if (r.job) store.set('facesJob', r.job);
        showSnackbar('Analisi volti avviata in background: puoi cambiare pagina liberamente.');
      } catch (e) {
        if (!disposed) showSnackbar(e.message || 'Impossibile avviare l’analisi volti.', { error: true });
      }
    });
  }

  function startRecluster() {
    return withStartLock(async () => {
      const ok = await confirmDialog({
        title: 'Ricalcolare i gruppi di persone?',
        message: 'PhotoScan raggrupperà nuovamente i volti non ancora assegnati manualmente. I nomi e le correzioni che hai fatto verranno mantenuti.',
        confirmLabel: 'Ricalcola',
      });
      if (!ok || disposed) return;
      try {
        const r = await api.post('/people/recluster', {});
        if (disposed) return;
        if (r.job) store.set('facesJob', r.job);
      } catch (e) {
        if (!disposed) showSnackbar(e.message || 'Impossibile avviare il ricalcolo.', { error: true });
      }
    });
  }

  // ---------- Azioni sulle persone ----------
  async function renamePerson(person) {
    const name = await promptDialog({
      title: 'Nome della persona',
      label: 'Nome',
      value: person.name || '',
      placeholder: 'Es. Marco, Nonna Anna…',
      confirmLabel: 'Salva',
    });
    if (name === null || disposed) return;
    try {
      const updated = await api.patch(`/people/${person.id}`, { name });
      if (disposed) return;
      const idx = people.findIndex((p) => p.id === person.id);
      if (idx >= 0) people[idx] = { ...people[idx], ...updated };
      if (detail && detail.person.id === person.id) {
        detail.person = { ...detail.person, ...updated };
      }
      showSnackbar(`Rinominata in «${updated.name || 'Senza nome'}».`);
      renderCurrent();
    } catch (e) {
      if (!disposed) showSnackbar(e.message || 'Impossibile salvare il nome.', { error: true });
    }
  }

  async function toggleHiddenPerson(person) {
    const nextHidden = !person.hidden;
    try {
      const updated = await api.patch(`/people/${person.id}`, { hidden: nextHidden });
      if (disposed) return;
      showSnackbar(nextHidden ? 'Persona nascosta.' : 'Persona ripristinata.');
      if (nextHidden && !showHidden && activePersonId === person.id) {
        activePersonId = null;
        detail?.ac.abort();
        detail = null;
      } else if (detail && detail.person.id === person.id) {
        detail.person = { ...detail.person, ...updated };
      }
      await reloadPeopleList();
    } catch (e) {
      if (!disposed) showSnackbar(e.message || 'Operazione non riuscita.', { error: true });
    }
  }

  // ---------- Helper unici di unione (chooseMergeTarget + performMerge) ----------
  /**
   * Determina la persona di destinazione e chiede conferma all'utente.
   * Se ci sono piu' nomi diversi (o forceChoice=true), apre choiceDialog; altrimenti confirmDialog
   * mostrando la persona principale e il totale foto risultante. Ritorna targetId oppure null se annullato.
   */
  async function chooseMergeTarget(list, { preferredTargetId = null, skipConfirm = false, forceChoice = false } = {}) {
    const unique = [];
    const seen = new Set();
    for (const p of list || []) {
      if (p && !seen.has(p.id)) {
        seen.add(p.id);
        unique.push(p);
      }
    }
    if (unique.length < 2) return null;

    const totalPhotos = sumPhotos(unique);
    const distinctNames = new Set(
      unique.map((p) => (p.name || '').trim().toLowerCase()).filter(Boolean),
    );
    const defaultDst = defaultMergeTarget(unique, preferredTargetId);

    if (distinctNames.size > 1 || forceChoice) {
      const ordered = [defaultDst, ...unique.filter((p) => p.id !== defaultDst.id)];
      const chosenId = await choiceDialog({
        title: `Unisci ${unique.length} persone`,
        message: distinctNames.size > 1
          ? `Più persone hanno un nome diverso. Scegli la persona principale da mantenere (risultato: fino a ${plural(totalPhotos, 'foto', 'foto')}):`
          : `Scegli la persona principale da mantenere (risultato: fino a ${plural(totalPhotos, 'foto', 'foto')}):`,
        options: ordered.map((p) => ({
          value: p.id,
          label: `${p.name || 'Senza nome'} (${plural(p.count, 'foto', 'foto')})`,
        })),
        confirmLabel: 'Unisci',
      });
      return chosenId || null;
    }

    if (!skipConfirm) {
      const others = unique.filter((p) => p.id !== defaultDst.id);
      const othersDesc = others.length === 1
        ? `«${others[0].name || 'Senza nome'}» (${plural(others[0].count, 'foto', 'foto')})`
        : `${others.length} altre persone`;
      const ok = await confirmDialog({
        title: unique.length === 2 ? 'Unire queste due persone?' : `Unire ${unique.length} persone?`,
        message: `Le foto di ${othersDesc} verranno unite in «${defaultDst.name || 'Senza nome'}» `
          + `(persona principale, totale risultante: fino a ${plural(totalPhotos, 'foto', 'foto')}).`,
        confirmLabel: 'Unisci',
      });
      if (!ok || disposed) return null;
    }
    return defaultDst.id;
  }

  /** Esegue l'unione di 2+ persone e offre lo snackbar con «Annulla» collegato a POST /people/merge/{log_id}/undo. */
  async function performMerge(list, {
    preferredTargetId = null,
    openMergedDetail = false,
    skipConfirm = false,
    forceChoice = false,
    clearMergeMode = false,
  } = {}) {
    const unique = [];
    const seen = new Set();
    for (const p of list || []) {
      if (p && !seen.has(p.id)) {
        seen.add(p.id);
        unique.push(p);
      }
    }
    if (unique.length < 2) return false;

    const targetId = await chooseMergeTarget(unique, { preferredTargetId, skipConfirm, forceChoice });
    if (!targetId || disposed) return false;

    try {
      const res = await api.post('/people/merge', { ids: unique.map((p) => p.id), into: targetId });
      if (disposed) return false;
      if (clearMergeMode) {
        mergeMode = false;
        selectedIds.clear();
      }
      if (openMergedDetail) activePersonId = targetId;
      const dst = unique.find((p) => p.id === targetId);
      const label = dst?.name ? `Persone unite in «${dst.name}».` : 'Persone unite.';
      const logId = res?.log_id;
      showSnackbar(label, logId ? {
        actionLabel: 'Annulla',
        onAction: async () => {
          try {
            await api.post(`/people/merge/${logId}/undo`, {});
            if (disposed) return;
            showSnackbar('Unione annullata.');
            await reloadPeopleList();
          } catch (err) {
            if (!disposed) showSnackbar(err.message || 'Impossibile annullare l’unione.', { error: true });
          }
        },
      } : undefined);
      await reloadPeopleList();
      return true;
    } catch (e) {
      if (!disposed) showSnackbar(e.message || 'Impossibile unire le persone.', { error: true });
      return false;
    }
  }

  async function mergeSelected() {
    const chosen = people.filter((p) => selectedIds.has(p.id));
    if (chosen.length < 2) return;
    await performMerge(chosen, { clearMergeMode: true });
  }

  /** Nasconde o mostra in blocco le persone selezionate (una PATCH per persona). */
  async function setHiddenSelected(hidden) {
    const chosen = people.filter((p) => selectedIds.has(p.id) && Boolean(p.hidden) !== hidden);
    if (!chosen.length) return;
    const results = await Promise.allSettled(chosen.map((p) => api.patch(`/people/${p.id}`, { hidden })));
    if (disposed) return;
    const failed = results.filter((r) => r.status === 'rejected').length;
    const done = chosen.length - failed;
    if (hidden && !showHidden && activePersonId !== null && chosen.some((p) => p.id === activePersonId)) {
      activePersonId = null;
      detail?.ac.abort();
      detail = null;
    }
    mergeMode = false;
    selectedIds.clear();
    if (failed) {
      showSnackbar(`${formatNumber(done)} aggiornate, ${formatNumber(failed)} non riuscite.`, { error: true });
    } else {
      showSnackbar(hidden
        ? (done === 1 ? 'Persona nascosta.' : `${formatNumber(done)} persone nascoste.`)
        : (done === 1 ? 'Persona ripristinata.' : `${formatNumber(done)} persone ripristinate.`));
    }
    await reloadPeopleList();
  }

  /** Segna tutte le coppie di un gruppo come «Diverse» sul backend (POST /people/not-same) con Annulla. */
  async function markGroupNotSame(members) {
    const pairs = [];
    for (let i = 0; i < members.length; i++) {
      for (let j = i + 1; j < members.length; j++) {
        pairs.push([members[i].id, members[j].id]);
      }
    }
    if (!pairs.length) return;
    try {
      await Promise.all(pairs.map(([a, b]) => api.post('/people/not-same', { a, b })));
      if (disposed) return;
      showSnackbar(pairs.length === 1 ? 'Segnate come persone diverse.' : 'Gruppo segnato come persone diverse.', {
        actionLabel: 'Annulla',
        onAction: async () => {
          try {
            await Promise.all(pairs.map(([a, b]) => api.del(`/people/not-same/${a}/${b}`)));
            if (!disposed) {
              showSnackbar('Segnalazione annullata.');
              await reloadPeopleList();
            }
          } catch (err) {
            if (!disposed) showSnackbar(err.message || 'Impossibile annullare.', { error: true });
          }
        },
      });
      await reloadPeopleList();
    } catch (e) {
      if (!disposed) showSnackbar(e.message || 'Impossibile salvare la segnalazione.', { error: true });
    }
  }

  /** Dialog visuale «Unisci con…» (usato dal dettaglio persona e come alternativa da tastiera al drag&drop sulle card). */
  function openMergePicker(currentPerson, { openMergedDetail = true } = {}) {
    const others = people.filter((p) => p.id !== currentPerson.id);
    if (!others.length) {
      showSnackbar('Non ci sono altre persone con cui unirla.');
      return;
    }
    const dlg = document.createElement('md-dialog');
    dlg.className = 'merge-picker-dialog';
    const headline = h('div', { slot: 'headline' }, `Unisci «${currentPerson.name || 'Senza nome'}» con…`);
    const searchTf = document.createElement('md-outlined-text-field');
    searchTf.className = 'merge-picker-search';
    searchTf.label = 'Cerca per nome';
    searchTf.placeholder = 'Filtra persone…';
    searchTf.append(icon('search', { slot: 'leading-icon' }));

    const listEl = h('div', { class: 'merge-picker-list', role: 'list' });
    const renderOptions = (q = '') => {
      const needle = q.trim().toLowerCase();
      const filtered = needle
        ? others.filter((p) => (p.name || 'senza nome').toLowerCase().includes(needle))
        : others;
      if (!filtered.length) {
        listEl.replaceChildren(h('p', { class: 'muted md-typescale-body-medium merge-picker-empty' }, 'Nessuna persona trovata.'));
        return;
      }
      listEl.replaceChildren(...filtered.map((p) => {
        const preferred = (p.name && !currentPerson.name) ? p.id : (currentPerson.name && !p.name ? currentPerson.id : p.id);
        const dst = defaultMergeTarget([currentPerson, p], preferred);
        const tot = sumPhotos([currentPerson, p]);
        const conflict = Boolean(currentPerson.name?.trim() && p.name?.trim()
          && currentPerson.name.trim().toLowerCase() !== p.name.trim().toLowerCase());
        const previewText = conflict
          ? `Risultato: sceglierai il nome · fino a ${plural(tot, 'foto', 'foto')}`
          : `Risultato: «${dst.name || 'Senza nome'}» · fino a ${plural(tot, 'foto', 'foto')}`;
        return h('button', {
          type: 'button',
          class: 'merge-picker-item',
          role: 'listitem',
          onclick: () => {
            dlg.close();
            performMerge([currentPerson, p], { preferredTargetId: preferred, openMergedDetail });
          },
        },
          h('div', { class: 'merge-picker-item__avatar' }, faceAvatar(p, 128)),
          h('div', { class: 'merge-picker-item__info' },
            h('span', { class: `md-typescale-title-small${p.name ? '' : ' person-card__name--unnamed'}` }, p.name || 'Senza nome'),
            h('span', { class: 'muted md-typescale-body-small' }, plural(p.count, 'foto', 'foto')),
            h('span', { class: 'merge-picker-item__preview md-typescale-label-small' }, previewText)),
          icon('merge', { class: 'merge-picker-item__icon' }));
      }));
    };
    searchTf.addEventListener('input', () => renderOptions(searchTf.value));
    renderOptions('');

    const content = h('div', { slot: 'content', class: 'merge-picker-content' }, searchTf, listEl);
    const actions = h('div', { slot: 'actions' },
      h('md-text-button', { onclick: () => dlg.close() }, 'Annulla'));
    dlg.append(headline, content, actions);
    dlg.addEventListener('closed', () => dlg.remove());
    document.body.append(dlg);
    dlg.show();
  }

  /** Dialog «Analizza»: confronta le foto di un gruppo suggerito (2 o piu' persone), con scelta di quali includere. */
  function openCompareSuggestion(group) {
    const members = Array.isArray(group?.members) ? group.members : [group?.a, group?.b].filter(Boolean);
    if (members.length < 2) return;
    const main = members[0]; // gia' ordinato dal backend: nominata / piu' foto
    const others = members.slice(1);
    const ordered = [...others, main]; // prima quelle da verificare, in fondo la principale
    const hasSim = typeof group?.similarity === 'number';
    const pct = hasSim ? Math.round(group.similarity * 100) : null;
    const checkedIds = new Set(members.map((m) => m.id));

    const cmpAc = new AbortController();
    const dlg = document.createElement('md-dialog');
    dlg.className = 'compare-dialog';
    let suspendedForViewer = false;
    let savedScrollTop = 0;

    const headline = h('div', { slot: 'headline', class: 'compare-dialog__headline' },
      h('span', {}, members.length === 2 ? 'Confronta persone' : `Confronta ${members.length} persone`),
      hasSim ? h('span', { class: 'compare-dialog__sim md-typescale-label-medium' }, `${pct}% simili`) : null);

    const mergeBtn = h('md-filled-button', {
      onclick: () => {
        const toMerge = members.filter((m) => checkedIds.has(m.id));
        if (toMerge.length < 2) return;
        dlg.close();
        performMerge(toMerge, { preferredTargetId: checkedIds.has(main.id) ? main.id : toMerge[0].id });
      },
    }, icon('merge', { slot: 'icon' }), members.length === 2 ? 'Unisci' : 'Unisci gruppo');

    const updateMergeBtn = () => {
      const sel = members.filter((m) => checkedIds.has(m.id));
      mergeBtn.disabled = sel.length < 2;
      const tot = sumPhotos(sel);
      mergeBtn.replaceChildren(
        icon('merge', { slot: 'icon' }),
        sel.length === 2 ? `Unisci (~${formatNumber(tot)} foto)` : `Unisci ${sel.length} (~${formatNumber(tot)} foto)`,
      );
    };

    const buildSection = (person, isPrimary) => {
      const roleLabel = isPrimary ? 'Principale' : 'Da verificare';
      const roleCls = isPrimary ? 'is-primary' : 'is-secondary';
      const state = { items: [], total: person.count || 0 };
      const gridEl = h('div', { class: 'compare-photos-grid' }, skeletonGrid(6, { tile: 120 }));
      const moreBtn = h('md-text-button', {
        class: 'compare-section__more',
        hidden: true,
        onclick: async () => {
          moreBtn.disabled = true;
          await loadMore();
          moreBtn.disabled = false;
        },
      }, 'Carica altre foto');

      const loadMore = async () => {
        if (state.items.length && state.items.length >= state.total) return [];
        const start = state.items.length;
        try {
          const res = await api.get(`/people/${person.id}/files`, { offset: start, limit: 24 }, cmpAc.signal);
          if (cmpAc.signal.aborted) return [];
          state.total = res.total;
          state.items.push(...res.items);
          if (start === 0) gridEl.replaceChildren();
          res.items.forEach((f, k) => {
            const idx = start + k;
            const tile = createTile(f, {
              size: 140,
              onOpen: async () => {
                try {
                  const { openViewer } = await import('./viewer.js');
                  savedScrollTop = content.scrollTop;
                  suspendedForViewer = true;
                  dlg.close();
                  openViewer({
                    items: [...state.items],
                    index: idx,
                    loadMore,
                    onClose: () => {
                      suspendedForViewer = false;
                      if (!disposed && dlg.isConnected) {
                        dlg.show();
                        requestAnimationFrame(() => { content.scrollTop = savedScrollTop; });
                      }
                    },
                  });
                } catch {
                  suspendedForViewer = false;
                  showSnackbar('Il visualizzatore non è disponibile.', { error: true });
                }
              },
            });
            gridEl.append(tile);
          });
          if (!state.items.length) {
            gridEl.replaceChildren(h('p', { class: 'muted md-typescale-body-small' }, 'Nessuna foto disponibile.'));
          }
          moreBtn.hidden = state.items.length >= state.total;
          return res.items;
        } catch (e) {
          if (e.name !== 'AbortError' && start === 0) {
            gridEl.replaceChildren(h('p', { class: 'muted md-typescale-body-small' }, 'Impossibile caricare le foto.'));
          }
          return [];
        }
      };

      loadMore();

      let checkEl = null;
      if (members.length > 2) {
        const cb = document.createElement('md-checkbox');
        cb.checked = checkedIds.has(person.id);
        cb.setAttribute('aria-label', `Includi ${person.name || 'Senza nome'} nell'unione`);
        cb.addEventListener('change', () => {
          if (cb.checked) checkedIds.add(person.id);
          else checkedIds.delete(person.id);
          updateMergeBtn();
        });
        checkEl = cb;
      }

      return h('section', { class: `compare-section ${roleCls}` },
        h('div', { class: 'compare-section__head' },
          checkEl,
          h('div', { class: 'compare-section__avatar' }, faceAvatar(person, 128)),
          h('div', { class: 'compare-section__meta' },
            h('div', { class: 'compare-section__title-row' },
              h('h3', { class: `md-typescale-title-medium${person.name ? '' : ' person-card__name--unnamed'}` },
                person.name || 'Senza nome'),
              h('span', { class: `compare-section__badge md-typescale-label-small ${roleCls}` }, roleLabel)),
            h('p', { class: 'muted md-typescale-body-small' }, plural(person.count, 'foto', 'foto')))),
        gridEl,
        moreBtn);
    };

    const contentParts = [];
    ordered.forEach((p, idx) => {
      if (idx > 0) {
        contentParts.push(h('div', { class: 'compare-dialog__divider', role: 'separator' },
          h('span', { class: 'md-typescale-label-small' }, idx === ordered.length - 1 ? 'confronta con' : 'e con')));
      }
      contentParts.push(buildSection(p, p.id === main.id));
    });
    const content = h('div', { slot: 'content', class: 'compare-dialog__content' }, ...contentParts);

    updateMergeBtn();
    const actions = h('div', { slot: 'actions', class: 'compare-dialog__actions' },
      h('md-text-button', { onclick: () => dlg.close() }, 'Chiudi'),
      h('md-outlined-button', {
        onclick: () => {
          dlg.close();
          markGroupNotSame(members);
        },
      }, 'Diverse'),
      mergeBtn);

    dlg.append(headline, content, actions);
    dlg.addEventListener('closed', () => {
      if (suspendedForViewer) return;
      cmpAc.abort();
      dlg.remove();
    });
    document.body.append(dlg);
    dlg.show();
  }

  // ---------- Dettaglio di una persona ----------
  function closeDetail() {
    detail?.ac.abort();
    detail = null;
    activePersonId = null;
    history.replaceState(null, '', '#/people');
    renderList();
  }

  async function loadDetailPage(d) {
    if (d.items.length && d.items.length >= d.total) return [];
    const start = d.items.length;
    try {
      const pageRes = await api.get(`/people/${d.person.id}/files`, { offset: start, limit: FILES_PAGE }, d.ac.signal);
      if (disposed || detail !== d) return [];
      d.total = pageRes.total;
      d.items.push(...pageRes.items);
      if (start === 0) d.grid.replaceChildren();
      pageRes.items.forEach((f, k) => {
        d.grid.append(buildPersonPhotoItem(d, f, start + k));
      });
      if (!d.items.length) {
        d.grid.replaceChildren(emptyState({
          icon: 'photo_library',
          title: 'Nessuna foto rimasta',
          message: 'Non ci sono più foto assegnate a questa persona.',
        }));
      }
      d.more.hidden = d.items.length >= d.total;
      d.countEl.textContent = plural(d.total, 'foto', 'foto');
      return pageRes.items;
    } catch (e) {
      if (disposed || e.name === 'AbortError' || detail !== d) return [];
      showSnackbar(e.message, { error: true });
      if (start === 0) d.grid.replaceChildren(errorState({ title: 'Impossibile caricare le foto', message: e.message }));
      return [];
    }
  }

  function updateSplitBar(d) {
    if (!d.splitBar) return;
    d.splitBar.hidden = !d.selectMode;
    d.grid.classList.toggle('is-select-mode', d.selectMode);
    if (d.selectBtn) {
      d.selectBtn.replaceChildren(
        icon(d.selectMode ? 'close' : 'checklist', { slot: 'icon' }),
        d.selectMode ? 'Fine selezione' : 'Seleziona volti',
      );
    }
    const n = d.selectedFaceIds.size;
    d.splitCountEl.textContent = n
      ? `${formatNumber(n)} ${n === 1 ? 'volto selezionato' : 'volti selezionati'}`
      : 'Tocca le foto da spostare in una nuova persona.';
    d.splitBtn.disabled = n === 0;
  }

  async function splitSelectedFaces(d) {
    const fids = [...d.selectedFaceIds];
    if (!fids.length) return;
    const ok = await confirmDialog({
      title: 'Spostare i volti in una nuova persona?',
      message: `${plural(fids.length, 'volto selezionato verrà spostato', 'volti selezionati verranno spostati')} da «${d.person.name || 'Senza nome'}» in una nuova persona separata.`,
      confirmLabel: 'Sposta in nuova persona',
    });
    if (!ok || disposed || detail !== d) return;
    try {
      const res = await api.post('/people/split', { person_id: d.person.id, face_ids: fids });
      if (disposed) return;
      d.selectMode = false;
      d.selectedFaceIds.clear();
      const newPid = res?.person_id;
      showSnackbar(`${plural(res?.moved || fids.length, 'volto spostato', 'volti spostati')} in una nuova persona.`, newPid ? {
        actionLabel: 'Apri nuova persona',
        onAction: () => {
          const found = people.find((p) => p.id === newPid);
          if (found) openDetail(found);
          else { activePersonId = newPid; reloadPeopleList(); }
        },
      } : undefined);
      await reloadPeopleList();
    } catch (e) {
      if (!disposed) showSnackbar(e.message || 'Impossibile spostare i volti.', { error: true });
    }
  }

  function buildPersonPhotoItem(d, file, idx) {
    const faceId = file.face_id;
    const checkEl = faceId
      ? h('span', { class: 'person-photo-item__check', 'aria-hidden': 'true' })
      : null;

    const syncSelectedVisual = (wrap) => {
      const sel = Boolean(faceId && d.selectedFaceIds.has(faceId));
      wrap.classList.toggle('is-selected', sel);
      if (checkEl) checkEl.replaceChildren(sel ? icon('check') : null);
    };

    let wrap;
    const tile = createTile(file, {
      size: 180,
      onOpen: () => {
        if (d.selectMode && faceId) {
          if (d.selectedFaceIds.has(faceId)) d.selectedFaceIds.delete(faceId);
          else d.selectedFaceIds.add(faceId);
          syncSelectedVisual(wrap);
          updateSplitBar(d);
        } else {
          openFileViewer(d, idx);
        }
      },
    });
    wrap = h('div', { class: 'person-photo-item', 'data-face-id': faceId ? String(faceId) : '' }, tile, checkEl);
    syncSelectedVisual(wrap);

    if (faceId) {
      const isCover = Number(d.person.cover_face_id) === Number(faceId);
      const coverBtn = h('md-icon-button', {
        class: isCover ? 'is-active-cover' : '',
        'aria-label': isCover ? 'Foto profilo attuale' : 'Usa come foto profilo',
        title: isCover ? 'Foto profilo attuale' : 'Usa come foto profilo',
        onclick: (e) => { e.stopPropagation(); setCoverFace(d.person, faceId); },
      }, icon('account_circle'));
      const notThisBtn = h('md-icon-button', {
        'aria-label': 'Non è questa persona',
        title: 'Non è questa persona',
        onclick: (e) => { e.stopPropagation(); unassignFace(d, file, wrap); },
      }, icon('person_remove'));
      const excludeBtn = h('md-icon-button', {
        'aria-label': 'Elimina volto',
        title: 'Elimina falso volto',
        onclick: (e) => { e.stopPropagation(); excludeFace(d, file, wrap); },
      }, icon('visibility_off'));
      wrap.append(h('div', { class: 'person-photo-item__tools' }, coverBtn, notThisBtn, excludeBtn));
    }
    return wrap;
  }

  async function setCoverFace(person, faceId) {
    try {
      const updated = await api.patch(`/people/${person.id}`, { cover_face_id: faceId });
      if (disposed) return;
      const idx = people.findIndex((p) => p.id === person.id);
      if (idx >= 0) people[idx] = { ...people[idx], ...updated };
      if (detail && detail.person.id === person.id) {
        detail.person = { ...detail.person, ...updated };
        if (detail.avatarEl) {
          detail.avatarEl.replaceChildren(
            faceAvatar(detail.person, 256),
            h('span', { class: 'person-detail-head__avatar-edit', 'aria-hidden': 'true' }, icon('photo_camera')),
          );
        }
        detail.grid.querySelectorAll('.is-active-cover').forEach((el) => {
          el.classList.remove('is-active-cover');
          el.title = 'Usa come foto profilo';
          el.setAttribute('aria-label', 'Usa come foto profilo');
        });
        const items = detail.grid.querySelectorAll('.person-photo-item');
        detail.items.forEach((f, i) => {
          if (Number(f.face_id) === Number(updated.cover_face_id) && items[i]) {
            const btn = items[i].querySelector('.person-photo-item__tools md-icon-button');
            if (btn) {
              btn.classList.add('is-active-cover');
              btn.title = 'Foto profilo attuale';
              btn.setAttribute('aria-label', 'Foto profilo attuale');
            }
          }
        });
      }
      showSnackbar('Foto profilo aggiornata.');
    } catch (e) {
      if (!disposed) showSnackbar(e.message || 'Impossibile aggiornare la foto profilo.', { error: true });
    }
  }

  /** Dialog visuale «Scegli foto profilo» con i ritagli dei volti della persona. */
  function openCoverPicker(person) {
    const pickAc = new AbortController();
    const dlg = document.createElement('md-dialog');
    dlg.className = 'cover-picker-dialog';
    const headline = h('div', { slot: 'headline' }, `Scegli foto profilo · ${person.name || 'Senza nome'}`);
    const gridEl = h('div', { class: 'cover-picker-grid' }, skeletonGrid(8, { tile: 96 }));
    const state = { offset: 0, total: person.count || 0 };
    const moreBtn = h('md-text-button', {
      class: 'compare-section__more',
      hidden: true,
      onclick: async () => {
        moreBtn.disabled = true;
        await loadFaces();
        moreBtn.disabled = false;
      },
    }, 'Carica altri volti');

    const loadFaces = async () => {
      try {
        const res = await api.get(`/people/${person.id}/files`, { offset: state.offset, limit: 48 }, pickAc.signal);
        if (pickAc.signal.aborted) return;
        state.total = res.total;
        if (state.offset === 0) gridEl.replaceChildren();
        state.offset += res.items.length;
        res.items.forEach((f) => {
          if (!f.face_id) return;
          const active = Number(f.face_id) === Number(person.cover_face_id);
          const btn = h('button', {
            type: 'button',
            class: `cover-picker-face${active ? ' is-active' : ''}`,
            title: active ? 'Foto profilo attuale' : 'Imposta come foto profilo',
            onclick: async () => {
              dlg.close();
              await setCoverFace(person, f.face_id);
            },
          },
            h('img', { src: fileUrl.faceCrop(f.face_id, 192), alt: '', loading: 'lazy', decoding: 'async' }),
            active ? h('span', { class: 'cover-picker-face__badge', 'aria-hidden': 'true' }, icon('check')) : null);
          gridEl.append(btn);
        });
        moreBtn.hidden = state.offset >= state.total;
      } catch (e) {
        if (e.name !== 'AbortError' && state.offset === 0) {
          gridEl.replaceChildren(h('p', { class: 'muted md-typescale-body-small' }, 'Impossibile caricare i volti.'));
        }
      }
    };
    loadFaces();

    const content = h('div', { slot: 'content', class: 'cover-picker-content' },
      h('p', { class: 'muted md-typescale-body-small', style: 'margin:0;' },
        'Tocca un volto per impostarlo come foto profilo della persona.'),
      gridEl,
      moreBtn);
    const actions = h('div', { slot: 'actions' },
      h('md-text-button', { onclick: () => dlg.close() }, 'Chiudi'));
    dlg.append(headline, content, actions);
    dlg.addEventListener('closed', () => { pickAc.abort(); dlg.remove(); });
    document.body.append(dlg);
    dlg.show();
  }

  async function unassignFace(d, file, itemEl) {
    const faceId = file.face_id;
    const personId = d.person.id;
    try {
      await api.post('/faces/assign', { face_ids: [faceId], person_id: null });
      if (disposed) return;
      itemEl.remove();
      d.selectedFaceIds.delete(faceId);
      d.total = Math.max(0, d.total - 1);
      d.countEl.textContent = plural(d.total, 'foto', 'foto');
      updateSplitBar(d);
      showSnackbar('Volto rimosso da questa persona.', {
        actionLabel: 'Annulla',
        onAction: async () => {
          try {
            await api.post('/faces/assign', { face_ids: [faceId], person_id: personId });
            if (!disposed) openDetail(d.person);
          } catch (err) {
            showSnackbar(err.message || 'Impossibile annullare.', { error: true });
          }
        },
      });
    } catch (e) {
      if (!disposed) showSnackbar(e.message || 'Operazione non riuscita.', { error: true });
    }
  }

  async function excludeFace(d, file, itemEl) {
    const faceId = file.face_id;
    try {
      await api.post(`/faces/${faceId}/exclude`, {});
      if (disposed) return;
      itemEl.remove();
      d.selectedFaceIds.delete(faceId);
      d.total = Math.max(0, d.total - 1);
      d.countEl.textContent = plural(d.total, 'foto', 'foto');
      updateSplitBar(d);
      showSnackbar('Volto escluso.', {
        actionLabel: 'Annulla',
        onAction: async () => {
          try {
            await api.post(`/faces/${faceId}/restore`, {});
            if (!disposed) openDetail(d.person);
          } catch (err) {
            showSnackbar(err.message || 'Impossibile ripristinare.', { error: true });
          }
        },
      });
    } catch (e) {
      if (!disposed) showSnackbar(e.message || 'Operazione non riuscita.', { error: true });
    }
  }

  async function openFileViewer(d, index) {
    try {
      const { openViewer } = await import('./viewer.js');
      openViewer({ items: [...d.items], index, loadMore: () => loadDetailPage(d) });
    } catch {
      showSnackbar('Il visualizzatore non è disponibile.', { error: true });
    }
  }

  function openDetail(person) {
    detail?.ac.abort();
    activePersonId = person.id;
    history.replaceState(null, '', `#/people?id=${person.id}`);
    const d = {
      person,
      items: [],
      total: person.count,
      ac: new AbortController(),
      selectMode: false,
      selectedFaceIds: new Set(),
    };
    detail = d;

    d.countEl = h('p', { class: 'muted md-typescale-body-medium' }, plural(person.count, 'foto', 'foto'));
    d.grid = h('div', { class: 'person-photos-grid' }, skeletonGrid(12, { tile: 170 }));
    d.more = h('md-text-button', {
      class: 'people-more',
      hidden: true,
      onclick: async () => {
        d.more.disabled = true;
        await loadDetailPage(d);
        if (detail === d) d.more.disabled = false;
      },
    }, 'Carica altre foto');

    d.avatarEl = h('button', {
      type: 'button',
      class: 'person-detail-head__avatar person-detail-head__avatar--btn',
      title: 'Cambia foto profilo',
      'aria-label': 'Cambia foto profilo',
      onclick: () => openCoverPicker(d.person),
    },
      faceAvatar(person, 256),
      h('span', { class: 'person-detail-head__avatar-edit', 'aria-hidden': 'true' }, icon('photo_camera')));

    d.selectBtn = h('md-outlined-button', {
      onclick: () => {
        d.selectMode = !d.selectMode;
        if (!d.selectMode) {
          d.selectedFaceIds.clear();
          d.grid.querySelectorAll('.person-photo-item.is-selected').forEach((el) => {
            el.classList.remove('is-selected');
            el.querySelector('.person-photo-item__check')?.replaceChildren();
          });
        }
        updateSplitBar(d);
      },
    }, icon('checklist', { slot: 'icon' }), 'Seleziona volti');

    d.splitCountEl = h('span', { class: 'md-typescale-body-medium' });
    d.splitBtn = h('md-filled-button', {
      disabled: true,
      onclick: () => splitSelectedFaces(d),
    }, icon('person_add', { slot: 'icon' }), 'Sposta in nuova persona');

    d.splitBar = h('div', { class: 'people-merge-bar person-split-bar', role: 'region', 'aria-label': 'Selezione volti', hidden: true },
      d.splitCountEl,
      h('div', { class: 'people-merge-bar__actions' },
        d.splitBtn,
        h('md-text-button', {
          onclick: () => {
            d.selectMode = false;
            d.selectedFaceIds.clear();
            d.grid.querySelectorAll('.person-photo-item.is-selected').forEach((el) => {
              el.classList.remove('is-selected');
              el.querySelector('.person-photo-item__check')?.replaceChildren();
            });
            updateSplitBar(d);
          },
        }, 'Annulla')));

    const head = h('div', { class: 'person-detail-head' },
      h('md-icon-button', { 'aria-label': 'Torna a tutte le persone', title: 'Torna a tutte le persone', onclick: closeDetail }, icon('arrow_back')),
      d.avatarEl,
      h('div', { class: 'person-detail-head__info' },
        h('h1', { class: 'person-detail-head__title md-typescale-headline-small' }, person.name || 'Senza nome'),
        d.countEl),
      h('div', { class: 'person-detail-head__actions' },
        h('md-outlined-button', { onclick: () => renamePerson(d.person) }, icon('edit', { slot: 'icon' }), 'Rinomina'),
        h('md-outlined-button', { onclick: () => openCoverPicker(d.person) }, icon('photo_camera', { slot: 'icon' }), 'Foto profilo'),
        d.selectBtn,
        people.length >= 2
          ? h('md-outlined-button', { onclick: () => openMergePicker(d.person) }, icon('merge', { slot: 'icon' }), 'Unisci con…')
          : null,
        h('md-filled-tonal-button', { onclick: () => navigate('/gallery', { person: person.id }) }, icon('photo_library', { slot: 'icon' }), 'Apri in Galleria'),
        h('md-text-button', { onclick: () => toggleHiddenPerson(d.person) },
          icon(person.hidden ? 'visibility' : 'visibility_off', { slot: 'icon' }),
          person.hidden ? 'Mostra' : 'Nascondi')));

    updateSplitBar(d);
    contentSlot.replaceChildren(head, d.splitBar, d.grid, d.more);
    loadDetailPage(d);
  }

  // ---------- Fascia suggerimenti (per gruppi) ----------
  function buildSuggestionsBox() {
    if (!suggestionGroups.length) return null;
    const cards = suggestionGroups.map((g) => {
      const members = g.members || [];
      const pct = Math.round((g.similarity || 0) * 100);
      const shown = members.slice(0, 4);
      const extra = members.length - shown.length;
      const tot = sumPhotos(members);
      const names = members.slice(0, 3).map((m) => m.name || 'Senza nome').join(' + ')
        + (members.length > 3 ? ` + altri ${members.length - 3}` : '');

      const avatarNodes = [];
      shown.forEach((m, i) => {
        if (i > 0 && members.length === 2) {
          avatarNodes.push(h('span', { class: 'people-suggest-card__link', 'aria-hidden': 'true' }, icon('compare_arrows')));
        }
        avatarNodes.push(h('div', { class: 'people-suggest-card__avatar', title: m.name || 'Senza nome' }, faceAvatar(m, 128)));
      });
      if (extra > 0) {
        avatarNodes.push(h('span', { class: 'people-suggest-card__more md-typescale-label-medium' }, `+${extra}`));
      }

      return h('div', { class: `people-suggest-card${members.length > 2 ? ' is-group' : ''}` },
        h('div', { class: `people-suggest-card__avatars${members.length > 2 ? ' is-stack' : ''}` }, ...avatarNodes),
        h('div', { class: 'people-suggest-card__info' },
          h('p', { class: 'people-suggest-card__names md-typescale-label-large', title: names }, names),
          h('p', { class: 'muted md-typescale-body-small' },
            `${members.length === 2 ? '' : `${members.length} persone · `}fino a ${plural(tot, 'foto', 'foto')} · ${pct}% simili`)),
        h('div', { class: 'people-suggest-card__actions' },
          h('md-filled-tonal-button', {
            onclick: () => performMerge(members, { preferredTargetId: members[0]?.id }),
          }, icon('merge', { slot: 'icon' }), members.length > 2 ? 'Unisci gruppo' : 'Unisci'),
          h('md-outlined-button', {
            onclick: () => openCompareSuggestion(g),
          }, icon('compare', { slot: 'icon' }), 'Analizza'),
          h('md-text-button', {
            onclick: () => markGroupNotSame(members),
          }, 'Diverse')));
    });

    if (mergeMode) {
      return h('details', { class: 'people-suggestions people-suggestions--collapsible' },
        h('summary', { class: 'people-suggestions__head people-suggestions__summary' },
          icon('auto_awesome'),
          h('span', { class: 'md-typescale-title-small' }, 'Suggerimenti di unione'),
          h('span', { class: 'people-suggestions__count md-typescale-label-small' }, String(suggestionGroups.length))),
        h('div', { class: 'people-suggestions__grid' }, ...cards));
    }

    return h('section', { class: 'people-suggestions', 'aria-label': 'Possibili persone uguali' },
      h('div', { class: 'people-suggestions__head' },
        icon('auto_awesome'),
        h('h2', { class: 'md-typescale-title-small' }, 'Potrebbero essere la stessa persona'),
        h('span', { class: 'people-suggestions__count md-typescale-label-small' }, String(suggestionGroups.length))),
      h('div', { class: 'people-suggestions__grid' }, ...cards));
  }

  // ---------- Griglia delle persone ----------
  function renderList() {
    const facesInfo = statusInfo?.faces || {};
    const header = h('div', { class: 'view-header' },
      h('div', {},
        h('h1', { class: 'md-typescale-headline-medium' }, 'Persone'),
        h('p', { class: 'muted md-typescale-body-medium' },
          'Volti riconosciuti localmente sul tuo PC. Dai un nome alle persone, usa il pulsante Unisci sulle card o trascina una card sopra un’altra.')));

    // Stato 1: disattivato nelle impostazioni
    if (settingsInfo && !settingsInfo.enable_faces) {
      contentSlot.replaceChildren(header, emptyState({
        icon: 'face',
        title: 'Riconoscimento volti non attivo',
        message: 'Attiva il riconoscimento volti nelle impostazioni e avvia l’analisi per raggruppare automaticamente le persone nelle tue foto. Tutto resta sul tuo computer.',
        actionLabel: 'Apri impostazioni',
        onAction: () => navigate('/settings', { tab: 'general' }),
      }));
      return;
    }

    // Stato 2: runtime non disponibile
    if (facesInfo.available === false) {
      contentSlot.replaceChildren(header, emptyState({
        icon: 'error_outline',
        title: 'Motore volti non disponibile',
        message: facesInfo.reason || 'Installa le dipendenze del riconoscimento volti per usare questa funzione.',
      }));
      return;
    }

    // Stato 3: modelli non scaricati
    if (facesInfo.models_ready === false) {
      contentSlot.replaceChildren(header, emptyState({
        icon: 'download',
        title: 'Modelli volti da scaricare',
        message: 'Scarica i modelli locali (~280 MB, una sola volta) per rilevare e riconoscere i volti nelle tue foto senza inviare nulla online.',
        actionLabel: 'Scarica modelli volti',
        onAction: startDownloadModels,
      }));
      return;
    }

    // Toolbar
    const actions = [];
    actions.push(h('md-filled-tonal-button', { onclick: startScanFaces }, icon('face_retouching_natural', { slot: 'icon' }), 'Analizza volti'));
    actions.push(h('md-outlined-button', {
      onclick: () => {
        mergeMode = !mergeMode;
        selectedIds.clear();
        renderList();
      },
    }, icon(mergeMode ? 'close' : 'checklist', { slot: 'icon' }), mergeMode ? 'Fine selezione' : 'Seleziona'));
    actions.push(h('md-text-button', {
      onclick: async () => {
        showHidden = !showHidden;
        await reloadPeopleList();
      },
    }, icon(showHidden ? 'visibility_off' : 'visibility', { slot: 'icon' }), showHidden ? 'Nascondi escluse' : 'Mostra nascoste'));
    actions.push(h('md-text-button', { onclick: startRecluster }, icon('refresh', { slot: 'icon' }), 'Ricalcola persone'));

    const toolbar = h('div', { class: 'people-toolbar' },
      h('p', { class: 'muted md-typescale-body-medium' },
        people.length ? plural(people.length, 'persona trovata', 'persone trovate') : ''),
      h('div', { class: 'people-toolbar__actions' }, ...actions));

    // Stato 4: nessuna persona ancora
    if (!people.length) {
      contentSlot.replaceChildren(header, toolbar, emptyState({
        icon: 'group',
        title: 'Nessuna persona trovata ancora',
        message: 'Avvia l’analisi dei volti qui sotto: procede in background anche mentre navighi nelle altre sezioni.',
        actionLabel: 'Analizza volti ora',
        onAction: startScanFaces,
      }));
      return;
    }

    const suggestionsBox = buildSuggestionsBox();

    // Selezione attiva: tramite la spunta su qualsiasi card oppure dal pulsante «Seleziona» in toolbar.
    const selecting = mergeMode || selectedIds.size > 0;
    const selectedPeople = selecting ? people.filter((p) => selectedIds.has(p.id)) : [];
    const selectedTot = sumPhotos(selectedPeople);
    const mergePreview = selectedPeople.length
      ? h('div', { class: 'people-merge-preview', 'aria-label': 'Persone selezionate' },
        ...selectedPeople.map((p) => h('span', { class: 'people-merge-chip' },
          h('span', { class: 'people-merge-chip__avatar' }, faceAvatar(p, 96)),
          h('span', { class: 'people-merge-chip__text md-typescale-label-medium' },
            `${p.name || 'Senza nome'} (${formatNumber(p.count)})`),
          h('button', {
            type: 'button',
            class: 'people-merge-chip__remove',
            'aria-label': `Rimuovi ${p.name || 'Senza nome'} dalla selezione`,
            title: 'Rimuovi dalla selezione',
            onclick: () => { selectedIds.delete(p.id); renderList(); },
          }, icon('close')))))
      : null;

    const selHidden = selectedPeople.filter((p) => p.hidden).length;
    const selVisible = selectedPeople.length - selHidden;
    const openCompareSelected = () => {
      if (selectedPeople.length < 2) return;
      const main = defaultMergeTarget(selectedPeople);
      const ordered = [main, ...selectedPeople.filter((p) => p.id !== main.id)];
      openCompareSuggestion({ members: ordered, similarity: null });
    };

    const floatBar = selecting
      ? h('div', { class: 'people-merge-bar people-float-bar', role: 'region', 'aria-label': 'Azioni sulle persone selezionate' },
        h('div', { class: 'people-merge-bar__row' },
          h('span', { class: 'md-typescale-body-medium' },
            selectedPeople.length
              ? `${formatNumber(selectedPeople.length)} ${selectedPeople.length === 1 ? 'persona selezionata' : 'persone selezionate'} · fino a ${plural(selectedTot, 'foto', 'foto')}`
              : 'Spunta le persone da selezionare: puoi unirle, analizzarle insieme o nasconderle.'),
          h('div', { class: 'people-merge-bar__actions' },
            h('md-filled-button', { disabled: selectedPeople.length < 2, onclick: mergeSelected },
              icon('merge', { slot: 'icon' }), 'Unisci'),
            h('md-filled-tonal-button', { disabled: selectedPeople.length < 2, onclick: openCompareSelected },
              icon('compare', { slot: 'icon' }), 'Analizza insieme'),
            h('md-outlined-button', { disabled: selVisible === 0, onclick: () => setHiddenSelected(true) },
              icon('visibility_off', { slot: 'icon' }), 'Nascondi'),
            selHidden > 0
              ? h('md-outlined-button', { onclick: () => setHiddenSelected(false) },
                icon('visibility', { slot: 'icon' }), 'Mostra')
              : null,
            h('md-text-button', { onclick: () => { mergeMode = false; selectedIds.clear(); renderList(); } }, 'Annulla'))),
        mergePreview)
      : null;

    const grid = h('div', { class: `people-grid${selecting ? ' is-selecting' : ''}`, role: 'list', 'aria-label': 'Persone' },
      ...people.map((p) => {
        const sel = selectedIds.has(p.id);
        const toggleSelect = () => {
          if (selectedIds.has(p.id)) selectedIds.delete(p.id);
          else selectedIds.add(p.id);
          renderList();
        };

        const selectCheckBtn = h('button', {
          type: 'button',
          class: `person-card__select-btn${sel ? ' is-checked' : ''}`,
          role: 'checkbox',
          'aria-checked': String(sel),
          'aria-label': sel ? `Deseleziona ${p.name || 'Senza nome'}` : `Seleziona ${p.name || 'Senza nome'}`,
          title: sel ? 'Deseleziona' : 'Seleziona persona',
          onclick: (e) => {
            e.stopPropagation();
            toggleSelect();
          },
        }, sel ? icon('check') : null);

        const openInSelectBtn = selecting
          ? h('md-icon-button', {
            class: 'person-card__open-btn',
            'aria-label': `Apri foto di ${p.name || 'Senza nome'}`,
            title: 'Apri foto',
            onclick: (e) => {
              e.stopPropagation();
              openDetail(p);
            },
          }, icon('open_in_new'))
          : null;

        const quickMergeBtn = !selecting && people.length >= 2
          ? h('md-icon-button', {
            class: 'person-card__merge-btn',
            'aria-label': `Unisci ${p.name || 'Senza nome'} con un’altra persona`,
            title: 'Unisci con…',
            onclick: (e) => {
              e.stopPropagation();
              openMergePicker(p, { openMergedDetail: false });
            },
          }, icon('merge'))
          : null;

        const card = h('div', {
          class: `person-card${sel ? ' is-selected' : ''}${p.hidden ? ' is-hidden-person' : ''}`,
          role: 'listitem',
          tabindex: '0',
          draggable: !selecting,
          'aria-pressed': selecting ? String(sel) : undefined,
          'aria-label': selecting
            ? `${p.name || 'Senza nome'}, ${plural(p.count, 'foto', 'foto')}${sel ? ', selezionata' : ''}`
            : `${p.name || 'Senza nome'}, ${plural(p.count, 'foto', 'foto')}`,
          onclick: () => (selecting ? toggleSelect() : openDetail(p)),
          onkeydown: (e) => {
            if (e.target !== card) return;
            if (e.key === 'Enter' || e.key === ' ') {
              e.preventDefault();
              if (selecting) toggleSelect();
              else openDetail(p);
            }
          },
        },
          selectCheckBtn,
          openInSelectBtn,
          quickMergeBtn,
          !selecting ? h('span', { class: 'person-card__drop-hint md-typescale-label-small', 'aria-hidden': 'true' }, 'Rilascia per unire') : null,
          h('div', { class: 'person-card__media' }, cachedFaceAvatar(p, 256)),
          h('div', { class: 'person-card__body' },
            h('p', { class: `person-card__name md-typescale-title-small${p.name ? '' : ' person-card__name--unnamed'}` },
              p.name || 'Senza nome'),
            h('p', { class: 'person-card__meta muted md-typescale-body-small' },
              `${plural(p.count, 'foto', 'foto')}${p.hidden ? ' · Nascosta' : ''}`)));

        if (!selecting) {
          card.addEventListener('dragstart', (e) => {
            draggedPersonId = p.id;
            card.classList.add('is-dragging');
            e.dataTransfer.effectAllowed = 'move';
            e.dataTransfer.setData('text/plain', String(p.id));
          });
          card.addEventListener('dragend', () => {
            draggedPersonId = null;
            card.classList.remove('is-dragging');
            grid.querySelectorAll('.is-drop-target').forEach((el) => el.classList.remove('is-drop-target'));
          });
          card.addEventListener('dragover', (e) => {
            if (draggedPersonId == null || draggedPersonId === p.id) return;
            e.preventDefault();
            e.dataTransfer.dropEffect = 'move';
            card.classList.add('is-drop-target');
          });
          card.addEventListener('dragleave', (e) => {
            if (!card.contains(e.relatedTarget)) card.classList.remove('is-drop-target');
          });
          card.addEventListener('drop', (e) => {
            e.preventDefault();
            card.classList.remove('is-drop-target');
            const srcId = draggedPersonId ?? Number(e.dataTransfer.getData('text/plain'));
            draggedPersonId = null;
            if (!srcId || srcId === p.id) return;
            const srcPerson = people.find((x) => x.id === srcId);
            if (!srcPerson) return;
            performMerge([srcPerson, p], { preferredTargetId: p.id });
          });
        }
        return card;
      }));

    contentSlot.replaceChildren(header, toolbar, suggestionsBox, grid, floatBar);
  }

  function renderCurrent() {
    if (activePersonId != null) {
      const found = people.find((p) => p.id === activePersonId);
      if (found) { openDetail(found); return; }
      activePersonId = null;
    }
    renderList();
  }

  async function reloadPeopleList() {
    try {
      const [data, groups] = await Promise.all([
        api.get('/people', showHidden ? { include_hidden: 1 } : null, sig),
        api.get('/people/suggestion-groups', null, sig).catch(() => []),
      ]);
      if (disposed) return;
      people = Array.isArray(data) ? data : [];
      suggestionGroups = Array.isArray(groups) ? groups : [];
      renderCurrent();
    } catch (e) {
      if (disposed || e.name === 'AbortError') return;
      showSnackbar(e.message || 'Impossibile caricare le persone.', { error: true });
    }
  }

  async function loadAll() {
    avatarCache.clear(); // dopo scansioni/reset gli id dei volti possono essere riusati: niente <img> vecchie
    contentSlot.replaceChildren(skeletonGrid(10, { tile: 170 }));
    try {
      const [st, cfg, ppl, groups] = await Promise.all([
        api.get('/status', null, sig),
        api.get('/settings', null, sig),
        api.get('/people', showHidden ? { include_hidden: 1 } : null, sig),
        api.get('/people/suggestion-groups', null, sig).catch(() => []),
      ]);
      if (disposed) return;
      statusInfo = st;
      settingsInfo = cfg;
      people = Array.isArray(ppl) ? ppl : [];
      suggestionGroups = Array.isArray(groups) ? groups : [];
      renderCurrent();
    } catch (e) {
      if (disposed || e.name === 'AbortError') return;
      contentSlot.replaceChildren(errorState({
        title: 'Impossibile caricare le persone',
        message: e.message,
        onRetry: loadAll,
      }));
    }
  }

  await loadAll();

  return () => {
    disposed = true;
    signal?.removeEventListener('abort', stop);
    ac.abort();
    detail?.ac.abort();
    unsubJob();
  };
}
