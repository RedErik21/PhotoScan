// Home: saluto, stato dell'archivio, ricordi, azioni rapide, aggiunte di recente e cifre.
import { api, fileUrl, onEvent } from '../api.js';
import { store } from '../store.js';
import { h, icon } from '../ui/el.js';
import { createWordmark } from '../ui/brand.js';
import { formatBytes, formatEta, formatNumber, plural } from '../ui/format.js';
import { createTile } from '../ui/photo-tile.js';
import { skeletonCard, skeletonGrid } from '../ui/skeleton.js';
import { emptyState, errorState } from '../ui/empty-state.js';
import { showSnackbar } from '../ui/snackbar.js';

const RECENT_LIMIT = 12;
const MOSAIC_CANDIDATES = 6;   // il mosaico ne mostra al massimo 3: se una miniatura non c'e', ne subentra un'altra

/** Carica il foglio di stile della view una sola volta (css/view-home.css). */
function loadCss() {
  if (document.querySelector('link[data-view-css="home"]')) return;
  const link = document.createElement('link');
  link.rel = 'stylesheet';
  link.href = new URL('../../css/view-home.css', import.meta.url).href;
  link.dataset.viewCss = 'home';
  document.head.append(link);
}

function greeting() {
  const hour = new Date().getHours();
  if (hour < 12) return 'Buongiorno';
  if (hour < 18) return 'Buon pomeriggio';
  return 'Buonasera';
}

/** Apre il visualizzatore; il modulo viene caricato solo al primo uso. */
async function openViewerSafe(items, index) {
  try {
    const { openViewer } = await import('./viewer.js');
    openViewer({ items, index });
  } catch {
    showSnackbar('Il visualizzatore non è disponibile.', { error: true });
  }
}

/** Card di invito/stato con titolo, testo, corpo facoltativo e un'azione.
 *  live: marcatore per aggiornare la card in place (index | plan); busy: disabilita l'azione durante una richiesta. */
function promoCard({ icon: name, title, text, body = null, action = null, onAction, prominent = false, busy = false, live = null }) {
  return h('article', {
    class: `card home-promo${prominent ? ' home-promo--prominent' : ''}`,
    dataset: live ? { live } : undefined,
  },
    h('div', { class: 'home-promo__head' },
      h('span', { class: 'home-promo__icon' }, icon(name)),
      h('div', { class: 'home-promo__copy' },
        h('h2', { class: 'home-promo__title' }, title),
        h('p', { class: 'home-promo__text' }, text)),
      action ? h('div', { class: 'home-promo__actions' },
        h('md-filled-button', { onclick: onAction, disabled: busy }, action)) : null),
    body);
}

/** Barra di avanzamento M3: valore 0..1, oppure indeterminata se il totale non è noto. */
function progressBar(fraction) {
  const known = typeof fraction === 'number' && isFinite(fraction);
  return h('md-linear-progress', {
    class: 'home-progress',
    value: known ? Math.min(1, Math.max(0, fraction)) : 0,
    indeterminate: !known,
    'aria-label': 'Avanzamento',
  });
}

/** Aggiorna una barra esistente (stessa logica di progressBar, senza ricrearla). */
function setBar(bar, fraction) {
  const known = typeof fraction === 'number' && isFinite(fraction);
  bar.indeterminate = !known;
  bar.value = known ? Math.min(1, Math.max(0, fraction)) : 0;
}

export async function mount(root, { navigate, signal }) {
  loadCss();

  const S = {
    status: store.get('status', null),
    index: null,
    dup: null,
    places: null,
    memories: null,
    recent: null,
    planJob: null,   // job di "Calcola le cartelle" in corso (dict job.public())
    planStarting: false, // richiesta /plan/rebuild in corso: blocca un secondo click
    failed: new Set(),   // sorgenti dell'ultimo caricamento andate in errore ('status', 'index', 'dup', 'places', 'mem', 'recent')
    loaded: false,
  };
  let alive = true;
  const shownErrors = new Set();

  // Contenitori persistenti: il testo dell'hero si aggiorna senza ricreare il mosaico (che ha un'animazione d'ingresso).
  const heroCopy = h('div', { class: 'home-hero__copy' });
  const heroMosaicSlot = h('div', { class: 'home-hero__mosaic-slot' });
  const hero = h('header', { class: 'home-hero' }, heroCopy, heroMosaicSlot);
  let mosaicKey = null;
  const indexSlot = h('section', { class: 'section home-index' });
  const memSlot = h('section', { class: 'section home-memories' });
  const actSlot = h('section', { class: 'section' });
  const recentSlot = h('section', { class: 'section' });
  const page = h('div', { class: 'view home' }, hero, indexSlot, actSlot, memSlot, recentSlot);
  root.append(page);

  /** Mostra un errore di rete/API una sola volta per messaggio. */
  const note = (err) => {
    if (!alive || err?.name === 'AbortError') return;
    const msg = err?.message || 'Operazione non riuscita';
    if (shownErrors.has(msg)) return;
    shownErrors.add(msg);
    showSnackbar(msg, { error: true });
  };

  /** Testo per un dato non ancora disponibile: "Caricamento…" oppure "Non disponibile" se la richiesta è fallita. */
  const pendingText = (key) => (S.failed.has(key) ? 'Non disponibile' : 'Caricamento…');

  // ---------- Sezioni ----------
  function renderHero() {
    const c = S.status?.counts;
    const sub = c
      ? (c.totale ? `Il tuo archivio conta ${formatNumber(c.totale)} ricordi.` : 'Il tuo archivio è pronto per essere riempito.')
      : (S.failed.has('status') ? 'Stato dell’archivio non disponibile al momento.' : 'Caricamento dell’archivio…');
    const dupFiles = S.dup ? S.dup.exact.files + S.dup.similar.files : null;
    const fmt = (v) => (v === null || v === undefined ? '—' : formatNumber(v));
    const stats = [
      ['photo', c?.foto, 'foto', '#/gallery?filter=photo'],
      ['videocam', c?.video, 'video', '#/gallery?filter=video'],
      ['location_on', S.places ? S.places.length : null, 'luoghi', '#/places'],
      ['filter_none', dupFiles, 'duplicati', '#/duplicates'],
    ];
    heroCopy.replaceChildren(
      h('div', { class: 'home-hero__brand-row' },
        createWordmark({ className: 'home-hero__wordmark', strokeWidth: 10.5, variant: 'hero', animated: true }),
        h('span', { class: 'home-hero__eyebrow-sep', 'aria-hidden': 'true' }, '·'),
        h('p', { class: 'home-hero__eyebrow' }, new Date().toLocaleDateString('it-IT', { weekday: 'long', day: 'numeric', month: 'long' }))),
      h('h1', { class: 'home-title' }, greeting()),
      h('p', { class: 'home-subtitle' }, sub),
      h('div', { class: 'home-hero__stats' }, stats.map(([ic, v, label, href]) =>
        h('a', { class: 'home-hero__stat', href },
          icon(ic),
          h('span', { class: 'home-hero__stat-value' }, fmt(v)),
          h('span', { class: 'home-hero__stat-label' }, label)))));

    // Mosaico: ricreato solo se cambia l'elenco delle foto (niente animazione ripetuta ad ogni aggiornamento).
    const cands = (S.recent || []).slice(0, MOSAIC_CANDIDATES);
    const key = cands.map((f) => f.id).join(',');
    if (key === mosaicKey) return;
    mosaicKey = key;
    heroMosaicSlot.replaceChildren();
    if (!cands.length) return;
    // Miniature che non si caricano: rimosse; il CSS mostra solo le prime tre che restano.
    const mosaicEl = h('div', { class: 'home-hero__mosaic', 'aria-hidden': 'true' },
      cands.map((f) => h('img', {
        src: fileUrl.thumbStrict(f.id, 512), alt: '', decoding: 'async',
        onerror: (e) => {
          e.currentTarget.remove();
          if (!mosaicEl.querySelector('img')) mosaicEl.remove();
        },
      })));
    heroMosaicSlot.append(mosaicEl);
  }

  function indexCardFor(st, ix, job) {
    const c = st.counts;
    if (st.first_run || c.totale === 0) {
      return promoCard({
        icon: 'add_photo_alternate', prominent: true,
        title: 'Benvenuto in PhotoScan',
        text: 'Scegli la cartella delle tue foto. L’app la legge senza mai modificarla.',
        action: 'Inizia', onAction: () => navigate(st.first_run ? '/onboarding' : '/index'),
      });
    }
    if (ix?.running) {
      const eta = formatEta(ix.eta_seconds);
      const detail = [ix.stage, ix.total ? `${formatNumber(ix.done)} di ${formatNumber(ix.total)}` : '', eta]
        .filter(Boolean).join(' · ');
      return promoCard({
        icon: 'hourglass_top', title: 'Analisi in corso', text: detail || 'Analisi delle foto…',
        body: progressBar(typeof ix.percent === 'number' ? ix.percent / 100 : null),
        action: 'Vedi dettagli', onAction: () => navigate('/index'),
        live: 'index',
      });
    }
    if (c.indicizzati === 0) {
      return promoCard({
        icon: 'photo_library', prominent: true,
        title: 'Archivio non ancora indicizzato',
        text: 'Serve un’analisi delle foto per cercarle e organizzarle.',
        action: 'Inizia', onAction: () => navigate('/index'),
      });
    }
    const errors = c.errori || 0;
    // Se l'analisi e' incompleta, la card "Organizza il tuo archivio" qui sotto mostra gia' il conteggio e il pulsante "Continua: Analisi".
    if (errors && c.totale - c.indicizzati - errors <= 0) {
      return promoCard({
        icon: 'error', title: plural(errors, 'file non leggibile', 'file non leggibili'),
        text: 'Non è stato possibile leggerli: puoi riprovare dalla pagina Analisi.',
        action: 'Vedi', onAction: () => navigate('/organize', { tab: 'index' }),
      });
    }
    return null;
  }

  /** Aggiorna in place la card "Analisi in corso" (evento ogni secondo): niente ricostruzione del DOM, niente sfarfallio. */
  function updateLiveIndex(card, ix) {
    const eta = formatEta(ix.eta_seconds);
    const detail = [ix.stage, ix.total ? `${formatNumber(ix.done)} di ${formatNumber(ix.total)}` : '', eta]
      .filter(Boolean).join(' · ');
    card.querySelector('.home-promo__text').textContent = detail || 'Analisi delle foto…';
    setBar(card.querySelector('md-linear-progress'), typeof ix.percent === 'number' ? ix.percent / 100 : null);
  }

  /** Come updateLiveIndex, per la card "Calcolo delle cartelle" (evento job ad ogni avanzamento). */
  function updateLivePlan(card, job) {
    card.querySelector('.home-promo__text').textContent = job.message || job.stage || 'Raggruppamento delle foto…';
    setBar(card.querySelector('md-linear-progress'), job.total ? job.done / job.total : null);
  }

  function renderIndex() {
    if (!alive) return;
    if (!S.status) {
      indexSlot.replaceChildren(S.loaded
        ? errorState({ title: 'Stato dell’archivio non disponibile', onRetry: refreshAll })
        : skeletonCard());
      return;
    }
    const c = S.status.counts;
    const canLive = !S.status.first_run && c.totale !== 0;
    const liveIx = indexSlot.querySelector('[data-live="index"]');
    if (liveIx && S.index?.running && canLive) {
      updateLiveIndex(liveIx, S.index);
      return;
    }
    const livePlan = indexSlot.querySelector('[data-live="plan"]');
    if (livePlan && S.planJob && !S.index?.running && canLive) {
      updateLivePlan(livePlan, S.planJob);
      return;
    }
    const card = indexCardFor(S.status, S.index, S.planJob);
    indexSlot.hidden = !card;
    if (card) indexSlot.replaceChildren(card);
    else indexSlot.replaceChildren();
  }

  // Chiave dell'ultimo elenco ricordi disegnato: se non cambia, il carosello (e la sua posizione di scorrimento) resta.
  let memKey = null;
  let memCarousel = null;

  function carouselStep(dir) {
    if (!memCarousel) return;
    const card = memCarousel.querySelector('.memory-card');
    const gap = parseFloat(getComputedStyle(memCarousel).columnGap) || 16;
    const step = (card ? card.offsetWidth : 240) + gap;
    const reduce = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    memCarousel.scrollBy({ left: dir * step, behavior: reduce ? 'auto' : 'smooth' });
  }

  /** Rotella verticale -> scorrimento orizzontale del carosello (finché c'è spazio da scorrere). */
  function onCarouselWheel(el, e) {
    if (Math.abs(e.deltaY) <= Math.abs(e.deltaX)) return;
    const max = el.scrollWidth - el.clientWidth;
    if (max <= 0) return;
    if ((e.deltaY < 0 && el.scrollLeft <= 0) || (e.deltaY > 0 && el.scrollLeft >= max)) return;
    e.preventDefault();
    el.scrollLeft += e.deltaY;
  }

  function renderMemories() {
    if (!alive) return;
    if (!S.loaded && !S.memories) {
      memSlot.hidden = false;
      memKey = null;
      memCarousel = null;
      memSlot.replaceChildren(h('div', { class: 'home-carousel', 'aria-busy': 'true' },
        [0, 1, 2].map(() => h('div', { class: 'skeleton memory-card-skel', 'aria-hidden': 'true' }))));
      return;
    }
    const groups = (S.memories || []).filter((g) => g.items?.length);
    memSlot.hidden = groups.length === 0;
    if (!groups.length) { memKey = null; memCarousel = null; memSlot.replaceChildren(); return; }
    const key = groups.map((g) => `${g.year}:${g.items.map((i) => i.id).join(',')}`).join('|');
    if (key === memKey && memCarousel && memSlot.contains(memCarousel)) return;
    memKey = key;
    const thisYear = new Date().getFullYear();
    const carousel = h('div', { class: 'home-carousel' }, groups.map((g) => memoryCard(g, thisYear)));
    carousel.addEventListener('wheel', (e) => onCarouselWheel(carousel, e), { passive: false });
    memCarousel = carousel;
    memSlot.replaceChildren(
      h('div', { class: 'home-section-head' },
        h('h2', { class: 'section-title' }, 'Ricordi'),
        h('div', { class: 'home-carousel-nav' },
          h('md-icon-button', { 'aria-label': 'Ricordo precedente', onclick: () => carouselStep(-1) }, icon('chevron_left')),
          h('md-icon-button', { 'aria-label': 'Ricordo successivo', onclick: () => carouselStep(1) }, icon('chevron_right')))),
      carousel);
  }

  /** Rimuove una tessera ricordo (nessuna foto caricabile); nasconde la sezione se non ne restano. */
  function dropMemoryCard(card) {
    card.remove();
    if (!memSlot.querySelector('.memory-card')) memSlot.hidden = true;
  }

  function memoryCard(group, thisYear) {
    const years = thisYear - group.year;
    const label = `${years} ${years === 1 ? 'anno' : 'anni'} fa`;
    // Striscia in basso: le foto dopo la copertina (la copertina non si ripete).
    const rest = group.items.slice(1, 5);
    let idx = 0;
    let card;
    // Se la copertina non si carica, passa alla foto successiva del gruppo; la tessera sparisce solo se nessuna si carica.
    const cover = h('img', {
      class: 'memory-card__cover', src: fileUrl.thumbStrict(group.items[0].id, 512), alt: '', loading: 'lazy', decoding: 'async',
      onerror: () => {
        idx += 1;
        if (idx < group.items.length) cover.src = fileUrl.thumbStrict(group.items[idx].id, 512);
        else dropMemoryCard(card);
      },
    });
    card = h('button', {
      class: 'memory-card',
      type: 'button',
      'aria-label': `${label}, ${plural(group.items.length, 'foto', 'foto')}. Apri`,
      onclick: () => openViewerSafe(group.items, 0),
    },
      cover,
      h('span', { class: 'memory-card__scrim', 'aria-hidden': 'true' }),
      h('span', { class: 'memory-card__text' },
        h('span', { class: 'memory-card__title md-typescale-title-medium' }, label),
        h('span', { class: 'memory-card__meta md-typescale-body-small' }, String(group.year))),
      rest.length ? h('span', { class: 'memory-card__strip', 'aria-hidden': 'true' },
        rest.map((f) => h('img', { src: fileUrl.thumbStrict(f.id, 128), alt: '', loading: 'lazy', decoding: 'async', onerror: (e) => e.currentTarget.remove() }))) : null);
    return card;
  }

  function renderActions() {
    if (!alive) return;
    const c = S.status?.counts;
    const plan = S.status?.plan;
    const d = S.dup;
    const ixDone = !!c && c.totale > 0 && c.indicizzati + (c.errori || 0) >= c.totale;
    const dupGroups = d ? d.exact.groups + d.similar.groups : null;
    // Foto arrivate dopo l'ultimo calcolo delle cartelle: il piano non le include ancora.
    const missing = Number(plan?.missing) || 0;
    const steps = [
      { id: 'index',   label: 'Analisi',       state: S.index?.running ? 'running' : ixDone ? 'done' : 'todo' },
      { id: 'cleanup', label: 'Libera spazio', state: !d ? 'todo' : (d.reviewed === true || dupGroups === 0) ? 'done' : 'warn' },
    ];
    const next = steps.find((st) => st.state !== 'done') || steps[1];
    const doneCount = steps.filter((st) => st.state === 'done').length;
    const pending = c ? Math.max(0, c.totale - c.indicizzati - (c.errori || 0)) : 0;
    const summary = doneCount === steps.length
      ? 'Archivio analizzato · puoi liberare altro spazio da video pesanti e screenshot.'
      : pending > 0
        ? `${plural(pending, 'file da analizzare', 'file da analizzare')} · ${doneCount} di ${steps.length} passi completati.`
        : `${doneCount} di ${steps.length} passi completati · prossimo: ${next.label}.`;
    actSlot.replaceChildren(
      h('div', { class: 'card home-org-card' },
        h('div', { class: 'home-org-card__head' },
          h('div', {},
            h('h2', { class: 'section-title' }, 'Organizza il tuo archivio'),
            h('p', { class: 'home-section-sub' }, summary)),
          h('md-filled-tonal-button', { onclick: () => navigate('/organize', { tab: next.id }) },
            doneCount === steps.length ? 'Apri Organizza' : `Continua: ${next.label}`,
            icon('arrow_forward', { slot: 'icon' }))),
        h('div', { class: 'home-org-pills' }, steps.map((st, idx) =>
          h('a', { class: `home-org-pill home-org-pill--${st.state}${st === next ? ' is-next' : ''}`, href: `#/organize?tab=${st.id}` },
            h('span', { class: 'home-org-pill__num' }, st.state === 'done' ? icon('check') : String(idx + 1)),
            h('span', {}, st.label))))));
  }

  function renderRecent() {
    if (!alive) return;
    const head = h('div', { class: 'home-section-head' },
      h('h2', { class: 'section-title' }, 'Aggiunte di recente'),
      h('md-text-button', { href: '#/gallery' }, 'Vedi tutte'));
    if (!S.recent) {
      if (S.failed.has('recent')) {
        recentSlot.replaceChildren(head, errorState({ title: 'Foto recenti non disponibili', onRetry: refreshAll }));
        return;
      }
      recentSlot.replaceChildren(head, skeletonGrid(RECENT_LIMIT, { tile: 148 }));
      return;
    }
    if (!S.recent.length) {
      recentSlot.replaceChildren(head, emptyState({
        icon: 'photo_library', title: 'Nessuna foto ancora',
        message: 'Quando l’archivio sarà indicizzato, le foto più recenti compariranno qui.',
      }));
      return;
    }
    const items = S.recent;
    recentSlot.replaceChildren(head,
      h('div', { class: 'tile-grid home-recent' },
        items.map((f, i) => createTile(f, { size: 148, onOpen: () => openViewerSafe(items, i) }))));
  }

  function renderAll() {
    renderHero();
    renderIndex();
    renderMemories();
    renderActions();
    renderRecent();
  }

  // ---------- Dati ----------
  /** Carica tutto in parallelo; ogni sorgente che fallisce non blocca le altre (e viene segnata in S.failed). */
  async function loadAll() {
    const names = ['status', 'index', 'dup', 'places', 'mem', 'recent'];
    const results = await Promise.allSettled([
      api.get('/status', undefined, signal),
      api.get('/index/status', undefined, signal),
      api.get('/duplicates/summary', undefined, signal),
      api.get('/places', { precision: 1 }, signal),
      api.get('/memories', { limit: 12 }, signal),
      api.get('/files', { limit: RECENT_LIMIT }, signal),
    ]);
    if (!alive) return;
    S.failed = new Set();
    const [st, ix, dup, places, mem, rec] = results.map((r, i) => {
      if (r.status === 'fulfilled') return r.value;
      S.failed.add(names[i]);
      note(r.reason);
      return undefined;
    });
    if (st) { S.status = st; store.set('status', st); }
    if (ix !== undefined) S.index = ix;
    if (dup !== undefined) S.dup = dup;
    if (places !== undefined) S.places = places;
    if (mem !== undefined) S.memories = mem;
    if (rec !== undefined) S.recent = rec.items || [];
    S.loaded = true;
  }

  async function refreshAll() {
    if (!alive) return;
    await loadAll();
    renderAll();
  }

  async function startPlan() {
    if (S.planStarting) return;
    S.planStarting = true;
    renderIndex();
    try {
      const { job_id } = await api.post('/plan/rebuild');
      S.planJob = { id: job_id, state: 'running', message: 'Avvio…', done: 0, total: 0 };
    } catch (e) {
      note(e);
    } finally {
      S.planStarting = false;
      renderIndex();
    }
  }

  // ---------- Eventi realtime ----------
  const offIndex = onEvent('index', (data) => {
    if (!alive) return;
    const wasRunning = S.index?.running;
    S.index = data;
    renderIndex();
    // A fine analisi cambiano duplicati, luoghi, ricordi e recenti: ricarica tutto, non solo lo stato.
    if (wasRunning && !data.running) refreshAll();
  });

  const offJob = onEvent('job', (job) => {
    if (!alive || !S.planJob || job.id !== S.planJob.id) return;
    if (job.state === 'running') { S.planJob = job; renderIndex(); return; }
    S.planJob = null;
    if (job.state === 'done') {
      showSnackbar('Cartelle calcolate.');
      refreshAll();
    } else if (job.state === 'error') {
      showSnackbar(job.error || 'Calcolo delle cartelle non riuscito.', { error: true });
      renderIndex();
    } else {
      renderIndex();
    }
  });

  const offStatus = store.subscribe('status', (st) => {
    if (!alive || !st || st === S.status) return;
    S.status = st;
    renderAll();
  });

  // ---------- Avvio ----------
  renderAll();
  await refreshAll();

  return () => {
    alive = false;
    offIndex();
    offJob();
    offStatus();
  };
}
