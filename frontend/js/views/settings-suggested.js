// Impostazioni > Suggerite: scoperta automatica dei temi ricorrenti ed gestione delle etichette da esempi.
// Le azioni scrivono subito sul server e notificano settings.js (onPlanStale) per offrire il ricalcolo del piano.
import { h, icon } from '../ui/el.js';
import { api, fileUrl, onEvent } from '../api.js';
import { showSnackbar } from '../ui/snackbar.js';
import { promptDialog, confirmDialog } from '../ui/dialog.js';
import { createTile } from '../ui/photo-tile.js';
import { skeletonGrid } from '../ui/skeleton.js';
import { emptyState } from '../ui/empty-state.js';
import { filterChips } from '../ui/chips.js';
import { formatNumber } from '../ui/format.js';

const SUGG_JOB = 'suggerimenti';
const DUR_SHORT = 160;
const EASE = 'cubic-bezier(.2, 0, 0, 1)';
const CSS_URL = new URL('../../css/view-settings-suggested.css', import.meta.url).href;

// Livelli di sensibilità per le etichette da esempi (soglia coseno).
const SENSITIVITY = [
  { id: 'broad', label: 'Ampia', thr: 0.78 },
  { id: 'balanced', label: 'Bilanciata', thr: 0.84 },
  { id: 'strict', label: 'Precisa', thr: 0.90 },
];

function loadCss() {
  if (document.querySelector('link[data-view-css="settings-suggested"]')) return;
  document.head.append(h('link', { rel: 'stylesheet', href: CSS_URL, dataset: { viewCss: 'settings-suggested' } }));
}

const reduceMotion = () => !!window.matchMedia?.('(prefers-reduced-motion: reduce)').matches;
function animateEl(el, frames, duration) {
  if (!el || reduceMotion() || typeof el.animate !== 'function') return null;
  return el.animate(frames, { duration, easing: EASE });
}

const fmtDate = (iso) => {
  const d = iso ? new Date(iso) : null;
  return d && !Number.isNaN(d.getTime()) ? d.toLocaleString('it-IT', { dateStyle: 'medium', timeStyle: 'short' }) : '';
};
const plural = (n, one, many) => `${formatNumber(n)} ${n === 1 ? one : many}`;

function closestSensitivity(thr) {
  let best = SENSITIVITY[1];
  let dist = Infinity;
  for (const s of SENSITIVITY) {
    const d = Math.abs((Number(thr) || 0.84) - s.thr);
    if (d < dist) { dist = d; best = s; }
  }
  return best.id;
}

function fadeOutAndRemove(el, onDone) {
  const a = animateEl(el, [{ opacity: 1, transform: 'none' }, { opacity: 0, transform: 'scale(.97)' }], DUR_SHORT);
  const finish = () => { el.remove(); onDone?.(); };
  if (a) a.finished.then(finish, finish);
  else finish();
}

export function mountSection(container, { onPlanStale = () => {} } = {}) {
  loadCss();
  let alive = true;
  let busyJobId = null;
  let hasSearched = false;
  let dismissedCount = 0;
  let closeSheet = null;
  const unsubs = [];

  // ---- barra principale
  const runBtn = h('md-filled-button', { onclick: () => runSearch() },
    icon('auto_awesome', { slot: 'icon' }), 'Cerca temi');
  const restoreBtn = h('md-text-button', { hidden: true, onclick: () => clearDismissed() },
    icon('restore', { slot: 'icon' }), 'Ripristina ignorati');
  const lastEl = h('p', { class: 'sugg-note md-typescale-body-small', 'aria-live': 'polite' });

  const progText = h('p', { class: 'md-typescale-body-small' }, '');
  const progEl = h('div', { class: 'sugg-progress', hidden: true, role: 'status' },
    h('md-linear-progress', { indeterminate: true }), progText);

  const stateEl = h('div', { class: 'sugg-state' });
  const cardsEl = h('div', { class: 'sugg-cards fs-stagger' });
  const visualEl = h('div', { class: 'sugg-visual fs-stagger' });
  const visualNone = h('p', { class: 'sugg-note md-typescale-body-medium', hidden: true },
    'Nessuna etichetta da esempi attiva: accettane una dai temi trovati sopra.');

  const root = h('div', { class: 'sugg-page' },
    h('section', { class: 'sugg-section' },
      h('div', { class: 'sugg-head' },
        h('div', { class: 'sugg-head__copy' },
          h('h3', { class: 'section-title' }, 'Temi trovati nelle tue foto'),
          h('p', { class: 'sugg-note md-typescale-body-medium' },
            'FotoScan cerca gruppi ricorrenti che compaiono in più giorni e mesi (palestra, ufficio, giardino…). '
            + 'I temi accettati creano cartelle dedicate per anno, es. «2024/Palestra».'),
          lastEl),
        h('div', { class: 'sugg-head__actions' }, restoreBtn, runBtn)),
      progEl, stateEl, cardsEl),
    h('section', { class: 'sugg-section' },
      h('h3', { class: 'section-title' }, 'Etichette da esempi'),
      h('p', { class: 'sugg-note md-typescale-body-medium' },
        'Temi già accettati. Puoi vedere le foto riconosciute, regolare la sensibilità o rinominarli.'),
      visualEl, visualNone));

  // ---- anteprima foto in bottom sheet + lightbox viewer
  function openPreviewSheet({ title, endpoint, returnEl }) {
    if (closeSheet) closeSheet();
    let done = false;
    let viewerOpen = false;
    const titleId = 'sugg-sheet-title';
    const body = h('div', { class: 'sugg-sheet__body', 'aria-live': 'polite' },
      h('md-linear-progress', { indeterminate: true }),
      skeletonGrid(18, { tile: 104 }));
    const closeBtn = h('md-icon-button', { 'aria-label': 'Chiudi', onclick: () => close() }, icon('close'));
    const scrim = h('div', { class: 'sugg-scrim', onclick: () => close() });
    const sheet = h('div', {
      class: 'sugg-sheet', role: 'dialog', 'aria-modal': 'true', 'aria-labelledby': titleId, tabindex: '-1',
    },
    h('div', { class: 'sugg-sheet__head' },
      h('h2', { id: titleId, class: 'md-typescale-title-large' }, title), closeBtn),
    body);

    const onKey = (e) => { if (e.key === 'Escape' && !viewerOpen) { e.preventDefault(); close(); } };
    function close() {
      if (done) return;
      done = true;
      document.removeEventListener('keydown', onKey, true);
      scrim.remove();
      sheet.remove();
      if (closeSheet === close) closeSheet = null;
      if (returnEl?.isConnected) returnEl.focus();
    }
    closeSheet = close;
    document.addEventListener('keydown', onKey, true);
    document.body.append(scrim, sheet);
    closeBtn.focus();

    async function openViewerAt(items, index) {
      viewerOpen = true;
      try {
        const { openViewer } = await import('./viewer.js');
        openViewer({ items, index, onClose: () => { viewerOpen = false; } });
      } catch {
        viewerOpen = false;
        showSnackbar('Visualizzatore non disponibile.', { error: true });
      }
    }

    api.get(endpoint, { limit: 36 }).then((data) => {
      if (done || !alive) return;
      const items = data?.items || [];
      const total = data?.total || items.length;
      const note = total > items.length
        ? `Mostro le prime ${formatNumber(items.length)} foto su ${formatNumber(total)} riconosciute.`
        : `${plural(total, 'foto riconosciuta', 'foto riconosciute')} per questo tema.`;
      body.replaceChildren(
        h('p', { class: 'sugg-note md-typescale-body-medium' }, note),
        items.length
          ? h('div', { class: 'tile-grid sugg-sheet__grid' },
            items.map((f, idx) => createTile(f, { size: 104, onOpen: () => openViewerAt(items, idx) })))
          : emptyState({ icon: 'image_search', title: 'Nessuna foto trovata' }));
    }).catch((e) => {
      if (done || !alive) return;
      body.replaceChildren(emptyState({ icon: 'error_outline', title: 'Anteprima non disponibile', message: e.message }));
    });
  }

  function thumbTile(id, size, onClick) {
    const img = h('img', { class: 'sugg-thumb', src: fileUrl.thumbStrict(id, size), alt: '', loading: 'lazy' });
    img.addEventListener('error', () => { img.hidden = true; });
    return h('button', {
      type: 'button', class: 'sugg-tile', 'aria-label': 'Apri anteprima foto del tema', onclick: onClick,
    }, img);
  }

  // ---- job SSE
  function follow(id) {
    busyJobId = id;
    runBtn.disabled = true;
    progEl.hidden = false;
    refreshState();
  }
  function setProgress(msg) { progText.textContent = msg || 'Ricerca dei temi in corso…'; }
  function stopFollowing() {
    busyJobId = null;
    progEl.hidden = true;
    runBtn.disabled = false;
    refreshState();
  }

  unsubs.push(onEvent('job', (job) => {
    if (!alive || job.name !== SUGG_JOB) return;
    if (busyJobId && job.id !== busyJobId) return;
    if (job.state === 'running') {
      follow(job.id);
      setProgress(job.message);
      return;
    }
    if (!busyJobId) return;
    stopFollowing();
    if (job.state === 'done') {
      showSnackbar('Ricerca dei temi completata.');
      loadSuggestions();
    } else {
      showSnackbar(job.state === 'cancelled' ? 'Ricerca annullata' : (job.error || 'Ricerca dei temi non riuscita'), { error: true });
    }
  }));

  async function runSearch() {
    if (busyJobId) return;
    runBtn.disabled = true;
    try {
      const res = await api.post('/labels/suggestions/run', {});
      if (alive) { follow(res.job_id); setProgress(); }
    } catch (e) {
      if (!alive) return;
      if (e.status === 409) { loadSuggestions(); return; }
      runBtn.disabled = false;
      showSnackbar(e.message || 'Ricerca non avviata.', { error: true });
    }
  }

  function refreshState() {
    const count = cardsEl.children.length;
    if (busyJobId || count > 0) {
      stateEl.replaceChildren();
      return;
    }
    if (!hasSearched) {
      stateEl.replaceChildren(emptyState({
        icon: 'auto_awesome',
        title: 'Scopri i temi ricorrenti del tuo archivio',
        message: 'Avvia una ricerca per individuare gruppi di foto che tornano spesso nel tempo e trasformarli in etichette automatiche.',
        actionLabel: 'Cerca temi ora',
        onAction: () => runSearch(),
      }));
    } else {
      stateEl.replaceChildren(emptyState({
        icon: 'check_circle',
        title: 'Nessun tema ricorrente nuovo',
        message: dismissedCount
          ? 'Hai già valutato tutti i temi trovati. Puoi ripristinare quelli ignorati per rivederli.'
          : 'Al momento non ci sono altri gruppi ricorrenti oltre alle tue etichette attuali.',
      }));
    }
  }

  function renderDismissedBtn() {
    restoreBtn.hidden = dismissedCount === 0;
    if (dismissedCount > 0) {
      restoreBtn.replaceChildren(icon('restore', { slot: 'icon' }), `Ripristina ignorati (${formatNumber(dismissedCount)})`);
    }
  }

  async function loadDismissed() {
    try {
      const r = await api.get('/labels/suggestions/dismissed');
      if (!alive) return;
      dismissedCount = r?.count || 0;
      renderDismissedBtn();
      refreshState();
    } catch { /* facoltativo */ }
  }

  async function clearDismissed() {
    const ok = await confirmDialog({
      title: 'Ripristinare i temi ignorati?',
      message: 'Alla prossima ricerca, i temi che avevi ignorato potranno essere proposti di nuovo.',
      confirmLabel: 'Ripristina',
    });
    if (!ok || !alive) return;
    try {
      await api.del('/labels/suggestions/dismissed');
      if (!alive) return;
      dismissedCount = 0;
      renderDismissedBtn();
      refreshState();
      showSnackbar('Temi ignorati ripristinati. Avvia «Cerca temi» per rivederli.', {
        actionLabel: 'Cerca ora', duration: 7000, onAction: () => runSearch(),
      });
    } catch (e) {
      if (alive) showSnackbar(e.message || 'Operazione non riuscita.', { error: true });
    }
  }

  // ---- card suggerimento
  function suggestionCard(item) {
    const field = h('md-outlined-text-field', { label: 'Nome etichetta', class: 'sugg-field' });
    field.value = item.name || '';
    field.addEventListener('input', () => { field.error = false; field.errorText = ''; });

    const alt = item.alt && item.alt !== item.name
      ? h('md-text-button', {
        class: 'sugg-alt',
        onclick: () => { field.value = item.alt; field.error = false; field.errorText = ''; },
      }, `Oppure: «${item.alt}»`)
      : null;

    const previewBtn = h('md-text-button', {
      onclick: () => openPreviewSheet({
        title: `Foto del tema «${field.value.trim() || item.name}»`,
        endpoint: `/labels/suggestions/${item.id}/preview`,
        returnEl: previewBtn,
      }),
    }, icon('visibility', { slot: 'icon' }), 'Vedi foto');

    const ignoreBtn = h('md-text-button', { onclick: () => ignore() }, 'Ignora');
    const acceptBtn = h('md-filled-tonal-button', { onclick: () => accept() },
      icon('label', { slot: 'icon' }), 'Aggiungi');

    const monthsPart = item.months ? ` in ${plural(item.months, 'mese', 'mesi')}` : '';
    const meta = `${formatNumber(item.count)} foto · ${plural(item.days, 'giorno', 'giorni')}${monthsPart}`;

    const openPreview = () => openPreviewSheet({
      title: `Foto del tema «${field.value.trim() || item.name}»`,
      endpoint: `/labels/suggestions/${item.id}/preview`,
      returnEl: previewBtn,
    });

    const card = h('article', { class: 'sugg-card', 'aria-label': `Tema ${item.name}` },
      h('div', { class: 'sugg-grid' }, (item.sample || []).slice(0, 9).map((id) => thumbTile(id, 128, openPreview))),
      field,
      alt,
      h('div', { class: 'sugg-meta-row' },
        h('p', { class: 'sugg-meta md-typescale-body-small' }, meta)),
      h('div', { class: 'sugg-actions' },
        previewBtn,
        h('div', { class: 'sugg-actions__right' }, ignoreBtn, acceptBtn)));

    const removeCard = () => fadeOutAndRemove(card, () => { refreshState(); loadVisual(); });

    async function accept() {
      const name = field.value.trim();
      acceptBtn.disabled = true;
      try {
        await api.post(`/labels/suggestions/${item.id}/accept`, { name });
        if (!alive) return;
        onPlanStale();
        showSnackbar(`Etichetta «${name}» aggiunta.`);
        removeCard();
      } catch (e) {
        if (!alive) return;
        acceptBtn.disabled = false;
        if (e.status === 404) { removeCard(); return; }
        field.error = true;
        field.errorText = e.message || 'Impossibile aggiungere l’etichetta.';
      }
    }

    async function ignore() {
      ignoreBtn.disabled = true;
      try {
        await api.post(`/labels/suggestions/${item.id}/dismiss`, {});
        if (!alive) return;
        dismissedCount += 1;
        renderDismissedBtn();
        showSnackbar(`Tema «${item.name}» ignorato.`);
        removeCard();
      } catch (e) {
        ignoreBtn.disabled = false;
        if (alive) showSnackbar(e.message || 'Operazione non riuscita.', { error: true });
      }
    }

    return card;
  }

  async function loadSuggestions() {
    try {
      const data = await api.get('/labels/suggestions');
      if (!alive) return;
      hasSearched = !!data.created;
      lastEl.textContent = data.created ? `Ultima ricerca: ${fmtDate(data.created)}` : '';
      cardsEl.replaceChildren(...(data.items || []).map((it) => suggestionCard(it)));
      if (data.job_id && busyJobId !== data.job_id) { follow(data.job_id); setProgress(); }
      refreshState();
    } catch (e) {
      if (alive) lastEl.textContent = `Non riesco a caricare i temi: ${e.message || 'errore'}`;
    }
  }

  // ---- etichette da esempi (accettate)
  function visualRow(v) {
    const previewBtn = h('md-icon-button', {
      'aria-label': `Vedi foto per ${v.name}`, title: 'Vedi foto riconosciute',
      onclick: () => openPreviewSheet({
        title: `Etichetta «${v.name}»`,
        endpoint: `/labels/visual/${encodeURIComponent(v.name)}/preview`,
        returnEl: previewBtn,
      }),
    }, icon('visibility'));
    const rename = h('md-icon-button', {
      'aria-label': 'Rinomina etichetta', title: 'Rinomina etichetta',
      onclick: () => renameVisual(v.name),
    }, icon('edit'));
    const del = h('md-icon-button', {
      'aria-label': 'Elimina etichetta', title: 'Elimina etichetta',
      onclick: () => deleteVisual(v.name),
    }, icon('delete'));

    const openPreview = () => openPreviewSheet({
      title: `Etichetta «${v.name}»`,
      endpoint: `/labels/visual/${encodeURIComponent(v.name)}/preview`,
      returnEl: previewBtn,
    });

    const sensChips = filterChips(
      SENSITIVITY.map((s) => ({ id: s.id, label: s.label })),
      {
        selected: closestSensitivity(v.thr),
        label: `Sensibilità per ${v.name}`,
        onChange: (id) => updateThreshold(v.name, id),
      },
    );

    return h('div', { class: 'sugg-vrow' },
      h('div', { class: 'sugg-vthumbs' }, (v.sample || []).slice(0, 4).map((id) => thumbTile(id, 64, openPreview))),
      h('div', { class: 'sugg-vtext' },
        h('span', { class: 'sugg-vname md-typescale-title-small' }, v.name),
        h('span', { class: 'sugg-meta md-typescale-body-small' }, plural(v.count, 'foto di esempio', 'foto di esempio'))),
      h('div', { class: 'sugg-vsens' },
        h('span', { class: 'sugg-vsens__label md-typescale-label-medium' }, 'Sensibilità'),
        sensChips),
      h('div', { class: 'sugg-vactions' }, previewBtn, rename, del));
  }

  async function updateThreshold(name, sensId) {
    const preset = SENSITIVITY.find((s) => s.id === sensId);
    if (!preset) return;
    try {
      await api.put(`/labels/visual/${encodeURIComponent(name)}`, { thr: preset.thr });
      if (!alive) return;
      onPlanStale();
      showSnackbar(`Sensibilità di «${name}» impostata su ${preset.label.toLowerCase()}.`);
    } catch (e) {
      if (alive) { showSnackbar(e.message || 'Aggiornamento non riuscito.', { error: true }); loadVisual(); }
    }
  }

  async function loadVisual() {
    try {
      const list = await api.get('/labels/visual');
      if (!alive) return;
      visualEl.replaceChildren(...list.map(visualRow));
      visualNone.hidden = list.length > 0;
    } catch (e) {
      if (alive) showSnackbar(e.message || 'Etichette da esempi non disponibili.', { error: true });
    }
  }

  async function renameVisual(name, { value = name, error = '' } = {}) {
    const next = await promptDialog({ title: 'Rinomina etichetta', label: 'Nome', value, confirmLabel: 'Salva', error });
    if (!next || next === name || !alive) return;
    try {
      await api.put(`/labels/visual/${encodeURIComponent(name)}`, { name: next });
      if (alive) { onPlanStale(); showSnackbar(`Etichetta rinominata in «${next}».`); loadVisual(); }
    } catch (e) {
      if (!alive) return;
      if (e.status === 404) { loadVisual(); return; }
      renameVisual(name, { value: next, error: e.message || 'Nome non valido.' });
    }
  }

  async function deleteVisual(name) {
    const ok = await confirmDialog({
      title: `Eliminare l’etichetta «${name}»?`,
      message: 'Le foto non vengono toccate; al prossimo calcolo del piano non avranno più questo tema.',
      confirmLabel: 'Elimina', danger: true,
    });
    if (!ok || !alive) return;
    try {
      await api.del(`/labels/visual/${encodeURIComponent(name)}`);
      if (alive) { onPlanStale(); loadVisual(); }
    } catch (e) {
      if (alive) showSnackbar(e.message || 'Eliminazione non riuscita.', { error: true });
    }
  }

  container.replaceChildren(root);
  loadSuggestions();
  loadVisual();
  loadDismissed();

  return {
    destroy() {
      alive = false;
      unsubs.forEach((fn) => fn());
      if (closeSheet) closeSheet();
    },
    setServerErrors() {},
    reveal() {},
  };
}
