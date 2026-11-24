// Persone: riconoscimento volti locale, rinomina, unione, nascondi e revisione foto per persona.
// Dati: GET /people, GET /people/{id}/files, PATCH /people/{id}, POST /people/merge,
//       POST /faces/assign, POST /faces/{id}/exclude, POST /faces/{id}/restore,
//       POST /people/recluster, POST /people/download-models.
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
const DISMISSED_KEY = 'fs_people_dismissed_pairs';

function pairKey(aId, bId) {
  const x = Number(aId);
  const y = Number(bId);
  return x < y ? `${x}:${y}` : `${y}:${x}`;
}

function loadDismissedPairs() {
  try {
    const raw = JSON.parse(localStorage.getItem(DISMISSED_KEY) || '[]');
    return new Set(Array.isArray(raw) ? raw.map(String) : []);
  } catch {
    return new Set();
  }
}

function saveDismissedPairs(set) {
  try {
    localStorage.setItem(DISMISSED_KEY, JSON.stringify([...set].slice(-300)));
  } catch { /* quota */ }
}

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

export async function mount(root, { navigate, query = {}, signal } = {}) {
  loadCss();
  const ac = new AbortController();
  const stop = () => ac.abort();
  signal?.addEventListener('abort', stop);
  const sig = ac.signal;

  let disposed = false;
  let people = [];
  let suggestions = [];
  const dismissedPairs = loadDismissedPairs();
  let statusInfo = null;
  let settingsInfo = null;
  let showHidden = false;
  let mergeMode = false;
  const selectedIds = new Set();
  let draggedPersonId = null;
  let activePersonId = query.id && /^\d+$/.test(String(query.id)) ? Number(query.id) : null;
  let detail = null;        // { person, items, total, ac }
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

  async function startDownloadModels() {
    try {
      const r = await api.post('/people/download-models', {});
      if (disposed) return;
      if (r.job) store.set('facesJob', r.job);
    } catch (e) {
      if (!disposed) showSnackbar(e.message || 'Impossibile scaricare i modelli.', { error: true });
    }
  }

  async function startScanFaces() {
    try {
      const r = await api.post('/people/scan', {});
      if (disposed) return;
      if (r.job) store.set('facesJob', r.job);
      showSnackbar('Analisi volti avviata in background: puoi cambiare pagina liberamente.');
    } catch (e) {
      if (!disposed) showSnackbar(e.message || 'Impossibile avviare l’analisi volti.', { error: true });
    }
  }

  async function startRecluster() {
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
      showSnackbar(`Rinominata in «${updated.name}».`);
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

  async function mergeSelected() {
    const ids = [...selectedIds];
    if (ids.length < 2) return;
    const chosen = people.filter((p) => selectedIds.has(p.id));
    const options = chosen.map((p) => ({
      value: p.id,
      label: `${p.name || 'Senza nome'} (${plural(p.count, 'foto', 'foto')})`,
    }));
    const targetId = await choiceDialog({
      title: `Unisci ${ids.length} persone`,
      message: 'Scegli quale persona mantenere come principale: tutte le foto delle altre verranno spostate qui.',
      options,
      confirmLabel: 'Unisci',
    });
    if (!targetId || disposed) return;
    try {
      await api.post('/people/merge', { ids, into: targetId });
      if (disposed) return;
      mergeMode = false;
      selectedIds.clear();
      showSnackbar('Persone unite.');
      await reloadPeopleList();
    } catch (e) {
      if (!disposed) showSnackbar(e.message || 'Impossibile unire le persone.', { error: true });
    }
  }

  /** Unisce due persone (usata da drag&drop, suggerimenti e «Unisci con…» nel dettaglio). */
  async function mergePair(personA, personB, { preferredTargetId = null, openMergedDetail = false, skipConfirm = false } = {}) {
    if (!personA || !personB || personA.id === personB.id) return;
    let targetId = preferredTargetId;
    const hasNameA = Boolean(personA.name && personA.name.trim());
    const hasNameB = Boolean(personB.name && personB.name.trim());

    if (hasNameA && hasNameB && personA.name.trim().toLowerCase() !== personB.name.trim().toLowerCase()) {
      targetId = await choiceDialog({
        title: 'Unisci due persone',
        message: 'Entrambe hanno già un nome. Quale vuoi mantenere come persona principale?',
        options: [
          { value: personA.id, label: `${personA.name} (${plural(personA.count, 'foto', 'foto')})` },
          { value: personB.id, label: `${personB.name} (${plural(personB.count, 'foto', 'foto')})` },
        ],
        confirmLabel: 'Unisci',
      });
      if (!targetId || disposed) return;
    } else {
      if (!targetId || (targetId !== personA.id && targetId !== personB.id)) {
        if (hasNameA && !hasNameB) targetId = personA.id;
        else if (hasNameB && !hasNameA) targetId = personB.id;
        else targetId = (personB.count || 0) > (personA.count || 0) ? personB.id : personA.id;
      }
      if (!skipConfirm) {
        const dst = targetId === personA.id ? personA : personB;
        const src = targetId === personA.id ? personB : personA;
        const ok = await confirmDialog({
          title: 'Unire queste due persone?',
          message: `Tutte le foto di «${src.name || 'Senza nome'}» (${plural(src.count, 'foto', 'foto')}) verranno unite in «${dst.name || 'Senza nome'}» (${plural(dst.count, 'foto', 'foto')}).`,
          confirmLabel: 'Unisci',
        });
        if (!ok || disposed) return;
      }
    }

    try {
      await api.post('/people/merge', { ids: [personA.id, personB.id], into: targetId });
      if (disposed) return;
      if (openMergedDetail) activePersonId = targetId;
      showSnackbar('Persone unite.');
      await reloadPeopleList();
    } catch (e) {
      if (!disposed) showSnackbar(e.message || 'Impossibile unire le persone.', { error: true });
    }
  }

  /** Dialog visuale «Unisci con…» dal dettaglio persona (con avatar e filtro per nome). */
  function openMergePicker(currentPerson) {
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
      listEl.replaceChildren(...filtered.map((p) => h('button', {
        type: 'button',
        class: 'merge-picker-item',
        role: 'listitem',
        onclick: () => {
          dlg.close();
          const preferred = (p.name && !currentPerson.name) ? p.id : (currentPerson.name && !p.name ? currentPerson.id : p.id);
          mergePair(currentPerson, p, { preferredTargetId: preferred, openMergedDetail: true });
        },
      },
        h('div', { class: 'merge-picker-item__avatar' }, faceAvatar(p, 128)),
        h('div', { class: 'merge-picker-item__info' },
          h('span', { class: `md-typescale-title-small${p.name ? '' : ' person-card__name--unnamed'}` }, p.name || 'Senza nome'),
          h('span', { class: 'muted md-typescale-body-small' }, plural(p.count, 'foto', 'foto'))),
        icon('merge', { class: 'merge-picker-item__icon' }))));
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

  /** Dialog «Analizza»: confronta le foto delle due persone suggerite (prima la non nominata / con meno foto, separata dalla principale). */
  function openCompareSuggestion(s) {
    const rank = (p) => [p.name && p.name.trim() ? 1 : 0, Number(p.count || 0)];
    const [ra, rb] = [rank(s.a), rank(s.b)];
    const aIsMain = ra[0] !== rb[0] ? ra[0] > rb[0] : ra[1] >= rb[1];
    const first = aIsMain ? s.b : s.a;   // non nominata o con meno foto
    const second = aIsMain ? s.a : s.b;  // nominata o con piu' foto
    const pct = Math.round((s.similarity || 0) * 100);

    const cmpAc = new AbortController();
    const dlg = document.createElement('md-dialog');
    dlg.className = 'compare-dialog';
    let suspendedForViewer = false;
    let savedScrollTop = 0;

    const headline = h('div', { slot: 'headline', class: 'compare-dialog__headline' },
      h('span', {}, 'Confronta persone'),
      h('span', { class: 'compare-dialog__sim md-typescale-label-medium' }, `${pct}% simili`));

    const buildSection = (person, roleLabel, roleCls) => {
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
                  // md-dialog usa il Top Layer del browser: lo sospendiamo mentre il visualizzatore e' aperto
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

      return h('section', { class: `compare-section ${roleCls}` },
        h('div', { class: 'compare-section__head' },
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

    const content = h('div', { slot: 'content', class: 'compare-dialog__content' },
      buildSection(first, 'Da verificare', 'is-secondary'),
      h('div', { class: 'compare-dialog__divider', role: 'separator' },
        h('span', { class: 'md-typescale-label-small' }, 'confronta con')),
      buildSection(second, 'Principale', 'is-primary'));

    const actions = h('div', { slot: 'actions', class: 'compare-dialog__actions' },
      h('md-text-button', { onclick: () => dlg.close() }, 'Chiudi'),
      h('md-outlined-button', {
        onclick: () => {
          dismissedPairs.add(pairKey(s.a.id, s.b.id));
          saveDismissedPairs(dismissedPairs);
          dlg.close();
          renderList();
        },
      }, 'Diverse'),
      h('md-filled-button', {
        onclick: () => {
          dlg.close();
          mergePair(second, first, { preferredTargetId: second.id });
        },
      }, icon('merge', { slot: 'icon' }), 'Unisci'));

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

  function buildPersonPhotoItem(d, file, idx) {
    const tile = createTile(file, {
      size: 180,
      onOpen: () => openFileViewer(d, idx),
    });
    const wrap = h('div', { class: 'person-photo-item' }, tile);
    if (file.face_id) {
      const isCover = Number(d.person.cover_face_id) === Number(file.face_id);
      const coverBtn = h('md-icon-button', {
        class: isCover ? 'is-active-cover' : '',
        'aria-label': isCover ? 'Foto profilo attuale' : 'Usa come foto profilo',
        title: isCover ? 'Foto profilo attuale' : 'Usa come foto profilo',
        onclick: (e) => { e.stopPropagation(); setCoverFace(d.person, file.face_id); },
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
        // Aggiorna evidenziazione pulsante "Foto profilo attuale" sulle foto gia' renderizzate
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
      d.total = Math.max(0, d.total - 1);
      d.countEl.textContent = plural(d.total, 'foto', 'foto');
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
      d.total = Math.max(0, d.total - 1);
      d.countEl.textContent = plural(d.total, 'foto', 'foto');
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
    const d = { person, items: [], total: person.count, ac: new AbortController() };
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

    const head = h('div', { class: 'person-detail-head' },
      h('md-icon-button', { 'aria-label': 'Torna a tutte le persone', title: 'Torna a tutte le persone', onclick: closeDetail }, icon('arrow_back')),
      d.avatarEl,
      h('div', { class: 'person-detail-head__info' },
        h('h1', { class: 'person-detail-head__title md-typescale-headline-small' }, person.name || 'Senza nome'),
        d.countEl),
      h('div', { class: 'person-detail-head__actions' },
        h('md-outlined-button', { onclick: () => renamePerson(d.person) }, icon('edit', { slot: 'icon' }), 'Rinomina'),
        h('md-outlined-button', { onclick: () => openCoverPicker(d.person) }, icon('photo_camera', { slot: 'icon' }), 'Foto profilo'),
        people.length >= 2
          ? h('md-outlined-button', { onclick: () => openMergePicker(d.person) }, icon('merge', { slot: 'icon' }), 'Unisci con…')
          : null,
        h('md-filled-tonal-button', { onclick: () => navigate('/gallery', { person: person.id }) }, icon('photo_library', { slot: 'icon' }), 'Apri in Galleria'),
        h('md-text-button', { onclick: () => toggleHiddenPerson(d.person) },
          icon(person.hidden ? 'visibility' : 'visibility_off', { slot: 'icon' }),
          person.hidden ? 'Mostra' : 'Nascondi')));

    contentSlot.replaceChildren(head, d.grid, d.more);
    loadDetailPage(d);
  }

  // ---------- Griglia delle persone ----------
  function renderList() {
    const facesInfo = statusInfo?.faces || {};
    const header = h('div', { class: 'view-header' },
      h('div', {},
        h('h1', { class: 'md-typescale-headline-medium' }, 'Persone'),
        h('p', { class: 'muted md-typescale-body-medium' },
          'Volti riconosciuti localmente sul tuo PC. Dai un nome alle persone o trascina una card sopra un’altra per unirle.')));

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
    if (people.length >= 2) {
      actions.push(h('md-outlined-button', {
        onclick: () => {
          mergeMode = !mergeMode;
          selectedIds.clear();
          renderList();
        },
      }, icon(mergeMode ? 'close' : 'merge', { slot: 'icon' }), mergeMode ? 'Esci da unione' : 'Unisci'));
    }
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

    // Fascia «Potrebbero essere la stessa persona»
    const visibleSuggestions = !mergeMode
      ? suggestions.filter((s) => !dismissedPairs.has(pairKey(s.a.id, s.b.id)))
      : [];
    const suggestionsBox = visibleSuggestions.length
      ? h('section', { class: 'people-suggestions', 'aria-label': 'Possibili persone uguali' },
        h('div', { class: 'people-suggestions__head' },
          icon('auto_awesome'),
          h('h2', { class: 'md-typescale-title-small' }, 'Potrebbero essere la stessa persona'),
          h('span', { class: 'people-suggestions__count md-typescale-label-small' }, String(visibleSuggestions.length))),
        h('div', { class: 'people-suggestions__grid' },
          ...visibleSuggestions.map((s) => {
            const pct = Math.round((s.similarity || 0) * 100);
            return h('div', { class: 'people-suggest-card' },
              h('div', { class: 'people-suggest-card__avatars' },
                h('div', { class: 'people-suggest-card__avatar' }, faceAvatar(s.a, 128)),
                h('span', { class: 'people-suggest-card__link', 'aria-hidden': 'true' }, icon('compare_arrows')),
                h('div', { class: 'people-suggest-card__avatar' }, faceAvatar(s.b, 128))),
              h('div', { class: 'people-suggest-card__info' },
                h('p', { class: 'people-suggest-card__names md-typescale-label-large' },
                  `${s.a.name || 'Senza nome'} + ${s.b.name || 'Senza nome'}`),
                h('p', { class: 'muted md-typescale-body-small' },
                  `${plural(s.a.count, 'foto', 'foto')} · ${plural(s.b.count, 'foto', 'foto')} · ${pct}% simili`)),
              h('div', { class: 'people-suggest-card__actions' },
                h('md-filled-tonal-button', {
                  onclick: () => mergePair(s.a, s.b, { preferredTargetId: s.a.id }),
                }, icon('merge', { slot: 'icon' }), 'Unisci'),
                h('md-outlined-button', {
                  onclick: () => openCompareSuggestion(s),
                }, icon('compare', { slot: 'icon' }), 'Analizza'),
                h('md-text-button', {
                  onclick: () => {
                    dismissedPairs.add(pairKey(s.a.id, s.b.id));
                    saveDismissedPairs(dismissedPairs);
                    renderList();
                  },
                }, 'Diverse')));
          })))
      : null;

    // Barra di unione quando mergeMode e' attivo
    const mergeBar = mergeMode
      ? h('div', { class: 'people-merge-bar', role: 'region', 'aria-label': 'Unisci persone' },
        h('span', { class: 'md-typescale-body-medium' },
          selectedIds.size
            ? `${formatNumber(selectedIds.size)} ${selectedIds.size === 1 ? 'persona selezionata' : 'persone selezionate'}`
            : 'Tocca due o più persone da unire in una sola.'),
        h('div', { class: 'people-merge-bar__actions' },
          h('md-filled-button', { disabled: selectedIds.size < 2, onclick: mergeSelected }, 'Unisci selezionate'),
          h('md-text-button', { onclick: () => { mergeMode = false; selectedIds.clear(); renderList(); } }, 'Annulla')))
      : null;

    const grid = h('div', { class: 'people-grid', role: 'list', 'aria-label': 'Persone' },
      ...people.map((p) => {
        const sel = mergeMode && selectedIds.has(p.id);
        const card = h('button', {
          type: 'button',
          class: `person-card${sel ? ' is-selected' : ''}${p.hidden ? ' is-hidden-person' : ''}`,
          role: 'listitem',
          draggable: !mergeMode,
          onclick: () => {
            if (mergeMode) {
              if (selectedIds.has(p.id)) selectedIds.delete(p.id); else selectedIds.add(p.id);
              renderList();
            } else {
              openDetail(p);
            }
          },
        },
          mergeMode ? h('span', { class: 'person-card__check', 'aria-hidden': 'true' }, sel ? icon('check') : null) : null,
          !mergeMode ? h('span', { class: 'person-card__drop-hint md-typescale-label-small', 'aria-hidden': 'true' }, 'Rilascia per unire') : null,
          h('div', { class: 'person-card__media' }, faceAvatar(p, 256)),
          h('div', { class: 'person-card__body' },
            h('p', { class: `person-card__name md-typescale-title-small${p.name ? '' : ' person-card__name--unnamed'}` },
              p.name || 'Senza nome'),
            h('p', { class: 'person-card__meta muted md-typescale-body-small' },
              `${plural(p.count, 'foto', 'foto')}${p.hidden ? ' · Nascosta' : ''}`)));

        if (!mergeMode) {
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
            mergePair(srcPerson, p, { preferredTargetId: p.id });
          });
        }
        return card;
      }));

    contentSlot.replaceChildren(header, toolbar, suggestionsBox, mergeBar, grid);
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
      const [data, sugg] = await Promise.all([
        api.get('/people', showHidden ? { include_hidden: 1 } : null, sig),
        api.get('/people/suggestions', null, sig).catch(() => []),
      ]);
      if (disposed) return;
      people = Array.isArray(data) ? data : [];
      suggestions = Array.isArray(sugg) ? sugg : [];
      renderCurrent();
    } catch (e) {
      if (disposed || e.name === 'AbortError') return;
      showSnackbar(e.message || 'Impossibile caricare le persone.', { error: true });
    }
  }

  async function loadAll() {
    contentSlot.replaceChildren(skeletonGrid(10, { tile: 170 }));
    try {
      const [st, cfg, ppl, sugg] = await Promise.all([
        api.get('/status', null, sig),
        api.get('/settings', null, sig),
        api.get('/people', showHidden ? { include_hidden: 1 } : null, sig),
        api.get('/people/suggestions', null, sig).catch(() => []),
      ]);
      if (disposed) return;
      statusInfo = st;
      settingsInfo = cfg;
      people = Array.isArray(ppl) ? ppl : [];
      suggestions = Array.isArray(sugg) ? sugg : [];
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
