// Indicizzazione: cruscotto con fasi, avanzamento in tempo reale, avvio/stop, log e file con problemi.
// Stato da GET /index/status + evento SSE 'index'; se SSE cade, polling ogni 2 s finche' non torna.
import { api, onEvent } from '../api.js';
import { store } from '../store.js';
import { h, icon } from '../ui/el.js';
import { formatEtaEnd, formatNumber } from '../ui/format.js';
import { confirmDialog } from '../ui/dialog.js';
import { showSnackbar } from '../ui/snackbar.js';
import { emptyState, errorState } from '../ui/empty-state.js';
import { skeletonCard } from '../ui/skeleton.js';

const BASE_TITLE = 'Analisi delle foto · PhotoScan';
const PHASES_BASE = ['Scansione', 'Metadati', 'Analisi immagini', 'Completato'];
const PHASES_FACES = ['Scansione', 'Metadati', 'Analisi immagini', 'Volti', 'Completato'];
const FAST_DEFAULT = 300;
const POLL_MS = 2000;
const ERR_LIMIT = 200;
const PIN_PX = 24;          // entro questa distanza dal fondo il log segue le novita'
const STATS = [
  { key: 'indicizzati', label: 'Indicizzati' },
  { key: 'foto', label: 'Foto' },
  { key: 'video', label: 'Video' },
  { key: 'duplicati', label: 'Copie identiche in più' },
  { key: 'errori', label: 'Con errori' },
  { key: 'senza_data_exif', label: 'Data stimata', hint: 'data presa dal nome o dalla data del file' },
];

/** Carica il foglio della view una sola volta (css/view-indexing.css). */
function loadCss() {
  if (document.querySelector('link[data-view-css="indexing"]')) return;
  const link = document.createElement('link');
  link.rel = 'stylesheet';
  link.href = new URL('../../css/view-indexing.css', import.meta.url).href;
  link.dataset.viewCss = 'indexing';
  document.head.append(link);
}

/** Indice della fase dal nome del backend (-1 = nessuna ancora). */
function phaseIndex(stage, withFaces) {
  switch (stage) {
    case 'scansione': return 0;
    case 'metadati': return 1;
    case 'embedding': return 2;
    case 'volti': return withFaces ? 3 : 2;
    case 'completato': return withFaces ? 4 : 3;
    default: return -1;
  }
}

/** Messaggio italiano per un errore del motore; il testo tecnico resta nel tooltip. */
function friendlyProblem(raw) {
  const t = String(raw || '');
  if (/fotogrammi|video|ffmpeg/i.test(t)) return 'Video non leggibile';
  if (/truncated|cannot identify|corrupt|broken|decodif/i.test(t)) return 'File danneggiato o incompleto';
  if (/hash|permission|denied|accesso/i.test(t)) return 'File non accessibile';
  if (/embedding/i.test(t)) return 'Analisi del contenuto non riuscita';
  return 'Problema durante la lettura';
}

/** Percorso abbreviato per la tabella: ultime tre parti, il resto diventa "…". */
function shortPath(p) {
  const parts = String(p || '').split(/[\\/]+/).filter(Boolean);
  return parts.length > 3 ? `…\\${parts.slice(-3).join('\\')}` : parts.join('\\');
}

/** Secondi -> '2:05' oppure '1:02:03'. */
function formatElapsed(sec) {
  sec = Math.max(0, Math.floor(sec));
  const hh = Math.floor(sec / 3600), mm = Math.floor((sec % 3600) / 60), ss = sec % 60;
  const p = (x) => String(x).padStart(2, '0');
  return hh ? `${hh}:${p(mm)}:${p(ss)}` : `${mm}:${p(ss)}`;
}

const errMsg = (e, fallback) => (e?.status === 0 ? 'Server non raggiungibile' : e?.message || fallback);

export async function mount(root, { navigate, signal }) {
  loadCss();
  const page = h('div', { class: 'view indexing', 'aria-busy': 'true' }, skeletonCard());
  root.append(page);

  let alive = true;
  let running = false;
  let wasRunning = false;
  let withFaces = false;
  let phases = PHASES_BASE;
  let started = null;          // epoch (s) dell'avvio corrente, null se non in corso
  let busy = false;            // richiesta start/stop/retry in corso
  let errCount = -1;           // ultimo numero di errori noto (per ricaricare la lista)
  let errRows = 0;
  let errTotal = 0;            // numero reale di file con errori (dallo stato, non dalla lista limitata)
  let autoscroll = true;
  let logSig = null;
  let pollTimer = null;
  const shown = new Set();
  const unsubs = [];

  /** Errore di rete/API una sola volta per messaggio. */
  const note = (e) => {
    if (!alive || e?.name === 'AbortError') return;
    const msg = errMsg(e, 'Operazione non riuscita');
    if (shown.has(msg)) return;
    shown.add(msg);
    showSnackbar(msg, { error: true });
  };

  // Riferimenti agli elementi aggiornati a ogni stato (creati una volta sola).
  const R = {};
  

  function buildPage() {
    R.title = h('h2', { class: 'idx-title md-typescale-title-large' });
    R.sub = h('p', { class: 'idx-sub muted md-typescale-body-medium' });
    R.steps = phases.map((label) => h('li', { class: 'idx-step is-todo' },
      h('span', { class: 'idx-step__dot', 'aria-hidden': 'true' }),
      h('span', { class: 'idx-step__label md-typescale-label-medium' }, label)));
    R.phase = h('span', {});
    R.percent = h('span', { class: 'idx-percent' });
    R.bar = h('md-linear-progress', { class: 'idx-bar', value: 0, 'aria-label': 'Avanzamento dell’indicizzazione' });
    R.count = h('p', { class: 'progress-block__text' });
    R.speed = h('dd', { class: 'idx-meta__value' }, '—');
    R.eta = h('dd', { class: 'idx-meta__value' }, '—');
    R.elapsed = h('dd', { class: 'idx-meta__value' }, '—');
    R.meta = h('dl', { class: 'idx-meta' });

    R.startText = h('span', {}, 'Avvia');
    R.start = h('md-filled-button', { onclick: () => startIndex() },
      icon('play_arrow', { slot: 'icon' }), R.startText);
    R.startOutText = h('span', {}, 'Cerca nuovi file');
    R.startOut = h('md-outlined-button', { onclick: () => startIndex(), hidden: true },
      icon('play_arrow', { slot: 'icon' }), R.startOutText);
    R.stop = h('md-outlined-button', { onclick: () => stopIndex(), hidden: true },
      icon('stop', { slot: 'icon' }), 'Ferma');

    R.fast = document.createElement('md-switch');
    R.fast.setAttribute('aria-label', 'Prova veloce');
    R.limit = document.createElement('md-outlined-text-field');
    R.limit.label = 'Numero di file da analizzare';
    R.limit.type = 'number';
    R.limit.min = '1';
    R.limit.value = String(FAST_DEFAULT);
    R.limit.style.display = 'none';
    R.fast.addEventListener('change', () => {
      R.limit.style.display = R.fast.selected ? '' : 'none';
    });
    R.subDir = document.createElement('md-outlined-text-field');
    R.subDir.label = 'Solo una sottocartella (facoltativa)';
    R.subDir.supportingText = 'Percorso dentro la cartella delle foto, per esempio 2020';

    R.done = h('div', { class: 'callout callout--info idx-done', role: 'status', hidden: true },
      icon('check_circle'),
      h('div', { class: 'idx-done__body' },
        h('h2', { class: 'callout__title md-typescale-title-medium' }, 'Prossimo passo: libera spazio'),
        h('p', {}, 'Ora puoi controllare duplicati, video pesanti e screenshot, oppure esplorare foto e album.'),
        h('div', { class: 'idx-done__actions' },
          h('md-filled-tonal-button', { onclick: () => navigate('/cleanup') }, 'Libera spazio'),
          h('md-text-button', { onclick: () => navigate('/gallery') }, 'Apri la galleria'))));

    R.nasBox = h('section', { class: 'section', hidden: true });
    R.rebaseBox = h('section', { class: 'section', hidden: true });

    R.statVals = {};
    R.statsNote = null;
    const statCards = STATS.map((s) => {
      R.statVals[s.key] = h('span', { class: 'stat-card__value' }, '—');
      return h('div', { class: 'card stat-card idx-stat', title: s.hint || undefined }, R.statVals[s.key],
        h('span', { class: 'stat-card__label' }, s.label),
        s.hint ? h('span', { class: 'idx-stat__hint muted' }, s.hint) : null);
    });

    R.logPre = h('pre', {
      class: 'code-block idx-log', role: 'log', tabindex: '0', 'aria-label': 'Registro dell’indicizzazione',
    });
    R.logPre.addEventListener('scroll', () => {
      autoscroll = R.logPre.scrollHeight - R.logPre.scrollTop - R.logPre.clientHeight < PIN_PX;
      updateLogToggle();
    });
    R.logToggle = h('md-text-button', { onclick: () => toggleLog() });

    R.errCount = h('span', { class: 'idx-count muted md-typescale-label-large' });
    R.errRetry = h('md-outlined-button', { onclick: () => retryErrors() }, icon('refresh', { slot: 'icon' }), 'Riprova');
    R.errNote = h('p', { class: 'muted md-typescale-body-medium' });
    R.errBody = h('tbody', {});
    R.errWrap = h('div', { class: 'idx-table-wrap' },
      h('table', { class: 'table-m3' },
        h('thead', {}, h('tr', {}, h('th', { scope: 'col' }, 'File'), h('th', { scope: 'col' }, 'Problema'), h('th', { scope: 'col' }, h('span', { class: 'sr-only' }, 'Azioni')))),
        R.errBody));
    R.errSection = h('section', { class: 'section idx-errors' },
      h('div', { class: 'idx-section-head' },
        h('h2', { class: 'section-title md-typescale-title-medium' }, 'File con problemi'),
        R.errCount, R.errRetry),
      R.errNote, R.errWrap);

    const main = h('section', { class: 'card idx-main' },
      h('div', { class: 'idx-head' }, R.title, R.sub),
      h('ol', { class: 'idx-stepper', 'aria-label': 'Fasi dell’indicizzazione' }, ...R.steps),
      h('div', { class: 'progress-block' },
        h('div', { class: 'progress-block__head' }, R.phase, R.percent),
        R.bar, R.count),
      (R.meta.replaceChildren(
        h('div', {}, h('dt', { class: 'idx-meta__label md-typescale-label-medium' }, 'Velocità (fase attuale)'), R.speed),
        h('div', {}, h('dt', { class: 'idx-meta__label md-typescale-label-medium' }, 'Tempo stimato per questa fase'), R.eta),
        h('div', {}, h('dt', { class: 'idx-meta__label md-typescale-label-medium' }, 'Trascorso'), R.elapsed)), R.meta),
      h('div', { class: 'idx-actions' }, R.start, R.startOut, R.stop),
      h('details', { class: 'idx-advanced' },
        h('summary', { class: 'md-typescale-label-large' }, 'Opzioni avanzate'),
        h('div', { class: 'idx-advanced__body' },
          h('label', { class: 'idx-switch' },
            h('span', { class: 'idx-switch__text' },
              h('span', { class: 'md-typescale-body-large' }, 'Prova veloce'),
              h('span', { class: 'muted md-typescale-body-medium' }, 'Analizza solo alcune foto, per provare.')),
            R.fast),
          R.limit,
          R.subDir)));

    R.logDetails = h('details', { class: 'idx-log-details' },
      h('summary', { class: 'idx-log-summary md-typescale-title-medium' }, 'Dettagli in tempo reale'),
      h('div', { class: 'idx-log-body' },
        h('div', { class: 'idx-log-tools' }, R.logToggle),
        R.logPre));
    const logSection = h('section', { class: 'section' }, R.logDetails);

    const statsSection = h('section', { class: 'section' },
      h('h2', { class: 'section-title md-typescale-title-medium' }, 'Riepilogo'),
      h('div', { class: 'idx-stats' }, ...statCards));

    page.replaceChildren(
      h('header', { class: 'view-header' },
        h('div', {},
          h('h1', { class: 'md-typescale-headline-medium idx-page-title' }, 'Analisi delle foto'),
          h('p', { class: 'muted md-typescale-body-medium' },
            'Legge le tue foto e le prepara per cercarle e organizzarle. Le foto originali non vengono mai modificate.'))),
      R.nasBox,
      R.rebaseBox,
      h('section', { class: 'section' }, main),
      h('section', { class: 'section', hidden: true }, R.done),
      statsSection,
      logSection,
      R.errSection);
    R.doneSection = R.done.parentElement;
    page.removeAttribute('aria-busy');
  }

  // ---------- Stato ----------
  function render(s) {
    if (!alive) return;
    const stage = s.stage || '';
    const stats = s.stats || {};
    const pct = typeof s.percent === 'number' ? s.percent : null;
    running = !!s.running;
    const phase = phaseIndex(stage, withFaces);
    const cur = running ? Math.max(phase, 0) : phase;      // -1 = mai avviata
    const completed = !running && stage === 'completato';
    const paused = !running && cur >= 0 && !completed;

    const netDown = s.error_code === 'network_unreachable'
      || (!!s.exit_code && (s.log_tail || []).some((l) => /non risponde/i.test(l)));

    // Titolo e messaggio
    if (running) {
      R.title.textContent = 'Analisi in corso';
      R.sub.textContent = 'Stiamo leggendo e analizzando le tue foto. Puoi usare l’app nel frattempo.';
    } else if (completed) {
      R.title.textContent = 'Analisi completata';
      R.sub.textContent = 'Tutte le foto sono pronte per essere organizzate.';
    } else if (paused || netDown) {
      if (netDown) {
        R.title.textContent = 'Disco di rete non raggiungibile';
        R.sub.textContent = 'Controlla che il NAS sia acceso e connesso, poi riprova: nessun file originale è stato toccato e il lavoro fatto non va perso.';
      } else {
        R.title.textContent = 'In pausa';
        R.sub.textContent = s.exit_code
          ? 'L’analisi si è interrotta. Nessun file originale è stato toccato: puoi riprendere quando vuoi.'
          : 'Hai fermato l’analisi. Puoi riprendere in qualsiasi momento, il lavoro fatto non va perso.';
      }
    } else {
      R.title.textContent = 'Analisi non ancora avviata';
      R.sub.textContent = 'Serve a cercare le foto e a proporre le cartelle. Non modifica i tuoi file.';
    }

    // Stato NAS preventivo (se sorgente su rete)
    renderNasNotice(s);
    // Avviso riallineamento percorso catalogo (se presente)
    renderRebaseNotice(s.last_rebase);

    // Stepper
    R.steps.forEach((li, i) => {
      const done = completed || (cur >= 0 && i < cur);
      const current = !completed && i === cur;
      li.className = `idx-step ${done ? 'is-done' : current ? 'is-current' : 'is-todo'}${current && paused ? ' is-paused' : ''}`;
      if (current) li.setAttribute('aria-current', 'step'); else li.removeAttribute('aria-current');
      const dot = li.firstChild;
      dot.replaceChildren();
      if (done) dot.append(icon('check'));
      else dot.textContent = String(i + 1);
    });

    // Avanzamento
    const label = cur >= 0 ? phases[cur] : '';
    R.phase.textContent = running ? `Fase: ${label}`
      : completed ? 'Tutto pronto'
        : paused ? `Interrotta nella fase: ${label}`
          : 'Nessuna analisi in corso';
    R.percent.textContent = completed ? '100%' : pct !== null ? `${Math.floor(pct)}%` : '';
    R.bar.indeterminate = running && pct === null;
    R.bar.value = completed ? 1 : pct !== null ? pct / 100 : 0;
    const total = s.total || 0, done = s.done || 0;
    // Nella fase 'completato' done/total sono 1/1 (segnaposto): mostriamo il totale reale dei file pronti
    R.count.textContent = stage === 'completato' ? `${formatNumber(stats.indicizzati ?? 0)} file pronti`
      : total ? `${formatNumber(done)} di ${formatNumber(total)} file`
        : running ? 'Ricerca dei file in corso…' : '';

    // Velocita' / stima / trascorso
    R.speed.textContent = running && s.rate_per_sec != null
      ? `${s.rate_per_sec.toLocaleString('it-IT', { maximumFractionDigits: 1 })} file/s` : '—';
    R.eta.textContent = running ? (formatEtaEnd(s.eta_seconds) || '—') : '—';
    R.meta.hidden = !running;
    started = running && s.started ? s.started : null;
    updateElapsed();

    // Pulsanti e opzioni
    R.startText.textContent = paused ? 'Riprendi' : 'Avvia';
    R.startOutText.textContent = 'Cerca nuovi file';
    R.start.hidden = running || completed;
    R.startOut.hidden = running || !completed;
    R.startOut.disabled = busy;
    R.stop.hidden = !running;
    R.start.disabled = busy;
    R.stop.disabled = busy;
    R.fast.disabled = running;
    R.limit.disabled = running;
    R.subDir.disabled = running;

    R.doneSection.hidden = !completed;
    R.done.hidden = !completed;

    // Riepilogo
    for (const k of Object.keys(R.statVals)) {
      R.statVals[k].textContent = stats[k] !== undefined ? formatNumber(stats[k]) : '—';
    }
    if (R.statsNote) {
      const tot = stats.totale ?? ((stats.foto ?? 0) + (stats.video ?? 0));
      R.statsNote.textContent = tot
        ? `${formatNumber(tot)} file trovati: ${formatNumber(stats.indicizzati ?? 0)} pronti, ${formatNumber(stats.errori ?? 0)} non leggibili`
        : '';
    }
    const errors = stats.errori ?? 0;
    errTotal = errors;
    if (errors !== errCount) { errCount = errors; loadErrors(); }

    // Log: aperto automaticamente mentre l'analisi gira, chiuso a riposo per ridurre il rumore visivo.
    if (running && R.logDetails && !R.logDetails.open) R.logDetails.open = true;
    const lines = s.log_tail || [];
    const sig = lines.join('\n');
    if (sig !== logSig) {
      logSig = sig;
      R.logPre.textContent = sig || 'Nessun messaggio ancora. Quando l’analisi parte, qui compaiono i dettagli.';
      if (autoscroll) R.logPre.scrollTop = R.logPre.scrollHeight;
    }

    // Fine di un'esecuzione: aggiorna lo stato globale (contatori della barra e della Home)
    if (wasRunning && !running) {
      api.get('/status').then((st) => { if (alive) store.set('status', st); }).catch(() => {});
      loadErrors();
    }
    wasRunning = running;

    // Titolo della scheda: percentuale mentre gira
    document.title = running
      ? (pct !== null ? `${Math.floor(pct)}% · ${BASE_TITLE}` : `Analisi in corso · PhotoScan`)
      : BASE_TITLE;
    updateButtons();
  }

  let lastNasSig = '';
  function renderNasNotice(s) {
    if (!R.nasBox) return;
    if (!s.source_network) {
      lastNasSig = '';
      R.nasBox.hidden = true;
      return;
    }
    const down = s.source_reachable === false;
    const sig = `${down ? 'down' : 'up'}|${s.running ? '1' : '0'}`;
    if (sig === lastNasSig) return;
    lastNasSig = sig;
    if (down) {
      const checkBtn = h('md-outlined-button', {
        onclick: async () => {
          checkBtn.disabled = true;
          try {
            const [freshIdx, freshSt] = await Promise.all([api.get('/index/status'), api.get('/status')]);
            if (!alive) return;
            store.set('status', freshSt);
            render(freshIdx);
            const ok = freshIdx.source_reachable !== false;
            showSnackbar(ok ? 'Disco di rete di nuovo raggiungibile.' : 'Il disco di rete non risponde ancora.', { error: !ok });
          } catch (e) {
            note(e);
          } finally {
            checkBtn.disabled = false;
          }
        },
      }, icon('network_check', { slot: 'icon' }), 'Verifica');
      R.nasBox.replaceChildren(
        h('div', { class: 'callout callout--warning', role: 'alert' },
          icon('dns'),
          h('div', { class: 'idx-done__body' },
            h('p', { class: 'callout__title' }, 'Disco di rete (NAS) non raggiungibile'),
            h('p', {}, 'Accendi o riconnetti il NAS prima di avviare l’analisi. Il catalogo esistente resta intatto.'),
            h('div', { class: 'idx-done__actions' }, checkBtn))));
      R.nasBox.hidden = false;
    } else if (!s.running) {
      R.nasBox.replaceChildren(
        h('div', { class: 'callout callout--info', role: 'status' },
          icon('lan'),
          h('div', {},
            h('p', { class: 'callout__title' }, 'Sorgente su disco di rete (NAS)'),
            h('p', {}, 'Il NAS è raggiungibile. Tienilo acceso durante l’analisi: in caso di disconnessione il catalogo non viene toccato.'))));
      R.nasBox.hidden = false;
    } else {
      R.nasBox.hidden = true;
    }
  }

  let lastRebaseSig = '';
  function renderRebaseNotice(rb) {
    if (!R.rebaseBox) return;
    if (!rb || !rb.at) {
      lastRebaseSig = '';
      R.rebaseBox.hidden = true;
      return;
    }
    const dismissKey = `fotoscan.rebase.dismissed.${rb.at}`;
    try {
      if (sessionStorage.getItem(dismissKey) === '1') {
        R.rebaseBox.hidden = true;
        return;
      }
    } catch { /* ignora */ }
    const sig = `${rb.at}|${rb.moved}`;
    if (sig === lastRebaseSig) return;
    lastRebaseSig = sig;
    const ok = (rb.moved || 0) > 0;
    const closeBtn = h('md-text-button', {
      onclick: () => {
        try { sessionStorage.setItem(dismissKey, '1'); } catch { /* ignora */ }
        R.rebaseBox.hidden = true;
      },
    }, 'Nascondi');
    R.rebaseBox.replaceChildren(
      h('div', { class: `callout ${ok ? 'callout--info' : 'callout--warning'}`, role: 'status' },
        icon(ok ? 'drive_file_move' : 'warning'),
        h('div', { class: 'idx-done__body' },
          h('p', { class: 'callout__title' }, ok ? 'Catalogo riallineato alla nuova cartella' : 'Percorso sorgente cambiato'),
          h('p', {}, ok
            ? `Aggiornati ${formatNumber(rb.moved)} percorsi da «${rb.from}» a «${rb.to}» senza reindicizzare.`
            : `La cartella «${rb.to}» non corrisponde a quella precedente («${rb.from}»): il catalogo non è stato riallineato e i nuovi file vengono indicizzati da zero.`),
          h('div', { class: 'idx-done__actions' }, closeBtn))));
    R.rebaseBox.hidden = false;
  }

  function updateElapsed() {
    R.elapsed.textContent = started ? formatElapsed(Date.now() / 1000 - started) : '—';
  }

  function updateButtons() {
    R.errRetry.disabled = busy || running || errRows === 0;
  }

  function updateLogToggle() {
    R.logToggle.textContent = 'Blocca scorrimento';
    R.logToggle.setAttribute('aria-pressed', String(!autoscroll));
  }

  function toggleLog() {
    autoscroll = !autoscroll;
    if (autoscroll) R.logPre.scrollTop = R.logPre.scrollHeight;
    updateLogToggle();
  }

  async function refresh() {
    if (!alive) return;
    try {
      const s = await api.get('/index/status');
      shown.clear();
      if (alive) render(s);
    } catch (e) { note(e); }
  }

  /** Apre il file (o lo mostra nella cartella) tramite il server; il file non viene mai modificato. */
  async function openFile(path, action) {
    try {
      await api.post('/system/open-file', { path, action });
    } catch (e) {
      showSnackbar(e.message || 'Impossibile aprire il file.', { error: true });
    }
  }

  async function loadErrors() {
    if (!alive) return;
    try {
      const rows = await api.get('/index/errors', { limit: ERR_LIMIT });
      if (!alive) return;
      errRows = rows.length;
      R.errBody.replaceChildren(...rows.map((r) => h('tr', {},
        h('td', { class: 'idx-path' }, h('span', { title: r.path }, shortPath(r.path))),
        h('td', { class: 'idx-problem', title: r.error || '' }, friendlyProblem(r.error)),
        h('td', { class: 'idx-row-actions' },
          h('md-icon-button', { 'aria-label': `Apri ${shortPath(r.path)}`, title: 'Apri con l’app predefinita', onclick: () => openFile(r.path, 'open') }, icon('open_in_new')),
          h('md-icon-button', { 'aria-label': `Mostra nella cartella ${shortPath(r.path)}`, title: 'Mostra nella cartella', onclick: () => openFile(r.path, 'reveal') }, icon('folder_open'))))));
      const total = Math.max(errTotal, rows.length);
      R.errCount.textContent = total ? `${formatNumber(total)} file` : '';
      R.errWrap.hidden = rows.length === 0;
      R.errNote.textContent = rows.length
        ? (rows.length >= ERR_LIMIT ? `Ne mostriamo i primi ${ERR_LIMIT}.` : 'Questi file non sono stati analizzati. Gli originali sono intatti.')
        : 'Nessun file con problemi. Tutto procede regolarmente.';
      updateButtons();
    } catch (e) { note(e); }
  }

  // ---------- Azioni ----------
  function setBusy(on) {
    busy = on;
    R.start.disabled = on;
    R.startOut.disabled = on;
    R.stop.disabled = on;
    updateButtons();
  }

  async function startIndex() {
    if (busy || running) return;
    const body = {};
    if (R.fast.selected) {
      const n = Number(R.limit.value);
      if (!Number.isInteger(n) || n < 1) {
        R.limit.error = true;
        R.limit.errorText = 'Inserisci un numero intero maggiore di zero';
        return;
      }
      R.limit.error = false;
      body.limit = n;
    }
    const sub = R.subDir.value.trim();
    if (sub) body.subdir = sub;
    setBusy(true);
    try {
      await api.post('/index/start', body);
      showSnackbar('Indicizzazione avviata. Puoi seguirla qui.');
    } catch (e) {
      showSnackbar(errMsg(e, 'Avvio non riuscito'), { error: true });
    } finally {
      await refresh();
      setBusy(false);
    }
  }

  async function stopIndex() {
    if (busy) return;
    const ok = await confirmDialog({
      title: 'Fermare l’indicizzazione?',
      message: 'Puoi riprendere in qualsiasi momento, il lavoro fatto non va perso.',
      confirmLabel: 'Ferma',
      cancelLabel: 'Continua',
    });
    if (!ok || !alive) return;
    setBusy(true);
    try {
      const r = await api.post('/index/stop');
      if (r.stopped) {
        showSnackbar('Indicizzazione ferma. Potrai riprenderla quando vuoi.');
      } else {
        const st = await api.get('/index/status');
        showSnackbar(st.running ? 'Sto fermando l’analisi… attendi qualche secondo.' : 'Non c’era nulla da fermare.');
      }
    } catch (e) {
      note(e);
    } finally {
      setBusy(false);
      refresh();
    }
  }

  async function retryErrors() {
    if (busy || running) return;
    setBusy(true);
    try {
      const r = await api.post('/index/retry-errors');
      const n = r.retried || 0;
      if (n === 0) showSnackbar('Nessun file da riprovare.');
      else showSnackbar(`${formatNumber(n)} ${n === 1 ? 'file rimesso' : 'file rimessi'} in coda.`,
        { actionLabel: 'Avvia', onAction: () => startIndex() });
      await loadErrors();
    } catch (e) {
      showSnackbar(errMsg(e, 'Nuovo tentativo non riuscito'), { error: true });
    } finally {
      setBusy(false);
      refresh();
    }
  }

  // ---------- Avvio ----------
  async function boot() {
    page.setAttribute('aria-busy', 'true');
    page.replaceChildren(skeletonCard());
    try {
      const [cfg, st, set] = await Promise.all([
        api.get('/status', undefined, signal),
        api.get('/index/status', undefined, signal),
        api.get('/settings', undefined, signal).catch(() => ({})),
      ]);
      if (!alive) return;
      withFaces = !!set?.enable_faces || st?.stage === 'volti';
      phases = withFaces ? PHASES_FACES : PHASES_BASE;
      if (!cfg.source_dir) {
        page.replaceChildren(emptyState({
          icon: 'folder_off',
          title: 'Nessuna cartella di foto scelta',
          message: 'Scegli la cartella con le tue foto nelle impostazioni, poi torna qui per avviare l’analisi.',
          actionLabel: 'Vai alle impostazioni',
          onAction: () => navigate('/settings'),
        }));
        return;
      }
      buildPage();
      render(st);
      unsubs.push(onEvent('index', (s) => { if (alive) render(s); }));
      unsubs.push(onEvent('connection', ({ online }) => {
        setPolling(!online);
        if (online) { shown.clear(); refresh(); }
      }));
    } catch (e) {
      if (e?.name === 'AbortError' || !alive) return;
      page.replaceChildren(errorState({
        title: 'Impossibile leggere lo stato dell’indicizzazione',
        message: errMsg(e, 'Riprova tra qualche istante.'),
        onRetry: () => boot(),
      }));
    }
  }

  function setPolling(on) {
    if (on && !pollTimer) pollTimer = setInterval(() => { if (!document.hidden) refresh(); }, POLL_MS);
    if (!on && pollTimer) { clearInterval(pollTimer); pollTimer = null; }
  }

  const ticker = setInterval(() => { if (alive && running && !document.hidden) updateElapsed(); }, 1000);
  // scheda nascosta: niente richieste; quando torna visibile si aggiorna subito
  const onVisible = () => { if (alive && !document.hidden && pollTimer) refresh(); };
  document.addEventListener('visibilitychange', onVisible);

  await boot();

  return () => {
    alive = false;
    clearInterval(ticker);
    document.removeEventListener('visibilitychange', onVisible);
    setPolling(false);
    unsubs.forEach((off) => off());
    document.title = BASE_TITLE;
  };
}
