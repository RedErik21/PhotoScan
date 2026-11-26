// Organizza: dashboard pulita di pulizia spazio + apertura a pieno schermo di ogni categoria
// (Analisi archivio, Duplicati identici/quasi uguali, Video pesanti, Screenshot, Documenti, Foto pesanti).
import { h, icon } from '../ui/el.js';
import { api, onEvent } from '../api.js';
import { store } from '../store.js';
import { formatBytes, formatNumber, plural } from '../ui/format.js';
import { skeletonCard } from '../ui/skeleton.js';
import { errorState } from '../ui/empty-state.js';

const CSS_URL = new URL('../../css/view-organize.css', import.meta.url).href;
function loadCss() {
  if (document.querySelector('link[data-view-css="organize"]')) return;
  document.head.append(h('link', { rel: 'stylesheet', href: CSS_URL, dataset: { viewCss: 'organize' } }));
}

const FOCUS_TITLES = {
  index: 'Analisi dell’archivio',
  duplicates: 'Duplicati',
  large_videos: 'Video pesanti',
  screenshots: 'Screenshot',
  documents: 'Documenti e scontrini',
  large_photos: 'Foto pesanti',
};

export async function mount(root, ctx) {
  loadCss();
  let alive = true;
  let seq = 0;
  let cleanupSub = null;
  let subAc = null;

  let status = store.get('status', null);
  let indexState = null;
  let cleanupSummary = null;

  // Determina se aprire subito una vista dedicata (deep-link) o la dashboard
  let focus = null; // null = dashboard | { kind: 'index' | 'duplicates' | 'cleanup', cat?, type?, view? }
  if (ctx.path === '/index' || ctx.query?.tab === 'index') {
    focus = { kind: 'index' };
  } else if (ctx.path === '/duplicates' || ctx.query?.cat === 'duplicates') {
    focus = { kind: 'duplicates', type: ctx.query?.type === 'similar' ? 'similar' : 'exact', view: ctx.query?.view || '' };
  } else if (ctx.query?.cat && ['large_videos', 'screenshots', 'documents', 'large_photos'].includes(ctx.query.cat)) {
    focus = { kind: 'cleanup', cat: ctx.query.cat };
  }

  const host = h('div', { class: 'view org-view' });
  root.replaceChildren(host);

  function teardownSub() {
    if (subAc) { subAc.abort(); subAc = null; }
    if (cleanupSub) {
      try { cleanupSub(); } catch (e) { console.error(e); }
      cleanupSub = null;
    }
  }

  function updateHash() {
    let hash = '#/organize';
    if (focus?.kind === 'index') hash = '#/organize?tab=index';
    else if (focus?.kind === 'duplicates') {
      const p = new URLSearchParams({ cat: 'duplicates', type: focus.type || 'exact' });
      if (focus.view) p.set('view', focus.view);
      hash = `#/organize?${p}`;
    } else if (focus?.kind === 'cleanup' && focus.cat) {
      hash = `#/organize?cat=${encodeURIComponent(focus.cat)}`;
    }
    if (location.hash !== hash) history.replaceState(null, '', hash);
  }

  function goHome() {
    focus = null;
    teardownSub();
    updateHash();
    renderDashboard();
    loadMeta();
  }

  function openFocus(next) {
    focus = next;
    updateHash();
    mountFocus();
  }

  // ---------- Dashboard principale (zero caos) ----------
  function renderDashboard() {
    if (!alive || focus !== null) return;
    const c = status?.counts;
    const running = !!indexState?.running;
    const remaining = c ? Math.max(0, (c.totale || 0) - (c.indicizzati || 0) - (c.errori || 0)) : 0;
    const idxReady = c && c.totale > 0 && remaining === 0 && !running;

    let idxText = 'Controllo stato dell’archivio…';
    let idxPill = '';
    let idxTone = 'neutral';
    if (running) {
      const p = typeof indexState.percent === 'number' ? `${Math.round(indexState.percent)}%` : 'in corso';
      idxText = `Analisi in corso (${p}) — puoi comunque esplorare l’app.`;
      idxPill = p;
      idxTone = 'running';
    } else if (idxReady) {
      idxText = `${formatNumber(c.indicizzati)} foto e video analizzati e pronti.`;
      idxPill = 'Pronto';
      idxTone = 'done';
    } else if (c) {
      idxText = c.totale
        ? `${formatNumber(remaining)} file ancora da analizzare su ${formatNumber(c.totale)}.`
        : 'Avvia la prima analisi per trovare duplicati e file pesanti.';
      idxPill = remaining ? `${formatNumber(remaining)} da fare` : 'Da avviare';
      idxTone = 'warn';
    }

    const indexBar = h('section', { class: 'org-index-bar' },
      h('div', { class: 'org-index-bar__left' },
        h('span', { class: `org-index-bar__icon is-${idxTone}`, 'aria-hidden': 'true' },
          running ? h('span', { class: 'org-tab__ring' }) : icon(idxReady ? 'check_circle' : 'manage_search')),
        h('div', { class: 'org-index-bar__copy' },
          h('div', { class: 'org-index-bar__title-row' },
            h('strong', { class: 'md-typescale-title-small' }, 'Analisi archivio'),
            idxPill ? h('span', { class: `org-pill org-pill--${idxTone}` }, idxPill) : null),
          h('span', { class: 'muted md-typescale-body-small' }, idxText))),
      h('md-outlined-button', { onclick: () => openFocus({ kind: 'index' }) },
        icon(running ? 'visibility' : 'play_arrow', { slot: 'icon' }),
        running ? 'Segui analisi' : (idxReady ? 'Cerca nuovi file' : 'Apri analisi')));

    const s = cleanupSummary;
    const dups = s?.duplicates || {};
    const cards = [
      {
        id: 'dup-exact',
        icon: 'filter_none',
        tone: 'primary',
        title: 'Duplicati identici',
        desc: 'Copie identiche bit per bit: puoi pulirle tutte in un clic tenendo 1 originale per gruppo.',
        bytes: dups.exact_bytes ?? dups.bytes ?? 0,
        meta: dups.exact_groups
          ? `${formatNumber(dups.exact_count)} copie · ${formatNumber(dups.exact_groups)} gruppi`
          : (dups.groups ? `${formatNumber(dups.count)} copie · ${formatNumber(dups.groups)} gruppi` : 'Nessun duplicato'),
        cta: 'Pulisci identici',
        onOpen: () => openFocus({ kind: 'duplicates', type: 'exact' }),
      },
      {
        id: 'dup-similar',
        icon: 'burst_mode',
        tone: 'primary',
        title: 'Foto quasi uguali',
        desc: 'Scatti a raffica o varianti simili: rivedili grandi 1 per volta e scegli quale tenere.',
        bytes: dups.similar_bytes ?? 0,
        meta: dups.similar_groups
          ? `${formatNumber(dups.similar_count)} foto · ${formatNumber(dups.similar_groups)} gruppi`
          : 'Nessuna foto simile',
        cta: 'Rivedi 1 per volta',
        onOpen: () => openFocus({ kind: 'duplicates', type: 'similar', view: 'focus' }),
      },
      {
        id: 'large_videos',
        icon: 'videocam',
        tone: 'warm',
        title: 'Video pesanti',
        desc: 'Video da almeno 50 MB, raggruppabili per data o cartella per capire subito quali eliminare.',
        bytes: s?.large_videos?.bytes ?? 0,
        meta: s?.large_videos ? plural(s.large_videos.count, 'video', 'video') : '—',
        cta: 'Rivedi video',
        onOpen: () => openFocus({ kind: 'cleanup', cat: 'large_videos' }),
      },
      {
        id: 'screenshots',
        icon: 'screenshot',
        tone: 'neutral',
        title: 'Screenshot',
        desc: 'Schermate salvate nel tempo, raggruppate per mese o cartella con selezione di gruppo.',
        bytes: s?.screenshots?.bytes ?? 0,
        meta: s?.screenshots ? plural(s.screenshots.count, 'screenshot', 'screenshot') : '—',
        cta: 'Rivedi screenshot',
        onOpen: () => openFocus({ kind: 'cleanup', cat: 'screenshots' }),
      },
      {
        id: 'documents',
        icon: 'receipt_long',
        tone: 'neutral',
        title: 'Documenti e scontrini',
        desc: 'Foto di fogli, ricevute e appunti che spesso non servono più.',
        bytes: s?.documents?.bytes ?? 0,
        meta: s?.documents ? plural(s.documents.count, 'file', 'file') : '—',
        cta: 'Rivedi documenti',
        onOpen: () => openFocus({ kind: 'cleanup', cat: 'documents' }),
      },
      {
        id: 'large_photos',
        icon: 'photo_size_select_large',
        tone: 'neutral',
        title: 'Foto pesanti',
        desc: 'Foto ad alta risoluzione da almeno 8 MB.',
        bytes: s?.large_photos?.bytes ?? 0,
        meta: s?.large_photos ? plural(s.large_photos.count, 'foto', 'foto') : '—',
        cta: 'Rivedi foto',
        onOpen: () => openFocus({ kind: 'cleanup', cat: 'large_photos' }),
      },
    ];

    const totalText = s
      ? (s.total_bytes ? `Libera fino a ${formatBytes(s.total_bytes)}` : 'Archivio già pulito')
      : 'Organizza e libera spazio';
    const subText = s
      ? `${plural(s.total_files, 'file candidato', 'file candidati')} · Ogni file eliminato finisce nel Cestino di Windows, mai cancellato definitivamente.`
      : 'Calcolo dello spazio recuperabile in corso…';

    const header = h('header', { class: 'view-header org-dash-head' },
      h('div', {},
        h('h1', { class: 't-headline' }, totalText),
        h('p', { class: 'muted t-body' }, subText)));

    const grid = h('div', { class: 'org-cards' },
      ...cards.map((card) => {
        const empty = s && card.bytes === 0 && card.meta.startsWith('Nessun');
        return h('article', {
          class: `card org-card${empty ? ' is-empty' : ''}`,
          onclick: () => card.onOpen(),
        },
        h('div', { class: 'org-card__top' },
          h('span', { class: `org-card__icon is-${card.tone}`, 'aria-hidden': 'true' }, icon(card.icon)),
          h('span', { class: 'org-card__bytes' }, s ? formatBytes(card.bytes) : '—')),
        h('h2', { class: 'org-card__title md-typescale-title-medium' }, card.title),
        h('p', { class: 'org-card__meta md-typescale-label-medium' }, card.meta),
        h('p', { class: 'org-card__desc muted md-typescale-body-small' }, card.desc),
        h('div', { class: 'org-card__foot' },
          h('md-filled-tonal-button', {
            onclick: (e) => { e.stopPropagation(); card.onOpen(); },
          }, card.cta, icon('arrow_forward', { slot: 'icon' }))));
      }));

    host.replaceChildren(header, indexBar, grid);
  }

  // ---------- Vista dedicata a pieno schermo ----------
  async function mountFocus() {
    const my = ++seq;
    teardownSub();
    const f = focus;
    if (!f) { renderDashboard(); return; }

    const title = f.kind === 'duplicates'
      ? (f.type === 'similar' ? 'Foto quasi uguali' : 'Duplicati identici')
      : (FOCUS_TITLES[f.cat || f.kind] || 'Organizza');

    const topbar = h('div', { class: 'org-focus-bar' },
      h('md-text-button', { onclick: () => goHome() }, icon('arrow_back', { slot: 'icon' }), 'Torna a Organizza'),
      h('h1', { class: 'org-focus-bar__title md-typescale-title-large' }, title));

    const bodyHost = h('div', { class: 'org-body' }, skeletonCard());
    host.replaceChildren(topbar, bodyHost);

    subAc = new AbortController();
    const myAc = subAc;
    const subNavigate = (path, query) => {
      if (path === '/index') { openFocus({ kind: 'index' }); return; }
      if (path === '/duplicates') { openFocus({ kind: 'duplicates', type: query?.type || 'exact', view: query?.view || '' }); return; }
      if (path === '/cleanup' && query?.cat) { openFocus({ kind: 'cleanup', cat: query.cat }); return; }
      if (path === '/cleanup') { goHome(); return; }
      ctx.navigate(path, query);
    };

    try {
      let mod;
      let subCtx = { ...ctx, signal: myAc.signal, navigate: subNavigate };
      if (f.kind === 'index') {
        mod = await import('./indexing.js');
        subCtx = { ...subCtx, path: '/index', query: ctx.query || {} };
      } else if (f.kind === 'duplicates') {
        mod = await import('./duplicates.js');
        subCtx = { ...subCtx, path: '/duplicates', query: { type: f.type || 'exact', view: f.view || '' } };
      } else {
        mod = await import('./cleanup.js');
        subCtx = { ...subCtx, path: '/cleanup', query: { cat: f.cat }, singleCategory: true };
      }
      if (!alive || my !== seq) return;
      bodyHost.replaceChildren();
      const res = await mod.mount(bodyHost, subCtx);
      if (!alive || my !== seq) { if (typeof res === 'function') res(); return; }
      if (typeof res === 'function') cleanupSub = res;
    } catch (e) {
      if (!alive || my !== seq) return;
      bodyHost.replaceChildren(errorState({ title: 'Sezione non disponibile', message: e.message, onRetry: () => mountFocus() }));
    }
  }

  async function loadMeta() {
    const [st, ix, cln] = await Promise.allSettled([
      api.get('/status', undefined, ctx.signal),
      api.get('/index/status', undefined, ctx.signal),
      api.get('/cleanup/summary', undefined, ctx.signal),
    ]);
    if (!alive) return;
    if (st.status === 'fulfilled') { status = st.value; store.set('status', st.value); }
    if (ix.status === 'fulfilled') indexState = ix.value;
    if (cln.status === 'fulfilled') cleanupSummary = cln.value;
    if (focus === null) renderDashboard();
  }

  const offIndex = onEvent('index', (s) => {
    if (!alive) return;
    const was = indexState?.running;
    indexState = s;
    if (focus === null) renderDashboard();
    if (was && !s?.running) loadMeta();
  });
  const offStatus = store.subscribe('status', (st) => {
    if (!alive || !st) return;
    status = st;
    if (focus === null) renderDashboard();
  });
  const onCleanupChanged = () => loadMeta();
  window.addEventListener('fotoscan:cleanup-changed', onCleanupChanged);

  if (focus) {
    loadMeta();
    await mountFocus();
  } else {
    renderDashboard();
    await loadMeta();
  }

  return () => {
    alive = false;
    seq++;
    teardownSub();
    offIndex();
    offStatus();
    window.removeEventListener('fotoscan:cleanup-changed', onCleanupChanged);
  };
}
