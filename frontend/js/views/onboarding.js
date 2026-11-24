// Benvenuto a schermo intero: 4 passi con stepper M3 (versione ridotta se l'archivio è già indicizzato).
// La view copre la shell con un overlay fisso (classe .ob in css/view-onboarding.css).
import { h, icon } from '../ui/el.js';
import { api } from '../api.js';
import { store } from '../store.js';
import { showSnackbar } from '../ui/snackbar.js';
import { formatNumber } from '../ui/format.js';

const CSS_HREF = new URL('../../css/view-onboarding.css', import.meta.url).href;
const STEPS = ['Benvenuto', 'Le tue foto', 'Dove creare le cartelle', 'Controllo e avvio'];
const GB = 1024 ** 3;
const LOW_SPACE = 5 * GB;
const QUICK_LIMIT = 300;

function loadCss() {
  if ([...document.querySelectorAll('link[rel="stylesheet"]')].some((l) => l.href === CSS_HREF)) return;
  const link = document.createElement('link');
  link.rel = 'stylesheet';
  link.href = CSS_HREF;
  document.head.append(link);
}

const fmtSize = (bytes) => (bytes >= GB
  ? `${(bytes / GB).toFixed(1).replace('.', ',')} GB`
  : `${Math.max(1, Math.round(bytes / 1024 ** 2))} MB`);

/** Normalizza un percorso Windows per i confronti (minuscole, separatori uniformi, niente barra finale). */
const normPath = (p) => String(p || '').trim().replace(/\//g, '\\').replace(/\\+$/, '').toLowerCase();
const isInside = (child, parent) => {
  const c = normPath(child);
  const p = normPath(parent);
  return Boolean(c && p) && (c === p || c.startsWith(`${p}\\`));
};
const sameDrive = (a, b) => Boolean(a && b) && a.toLowerCase() === b.toLowerCase();

/** Icona decorativa (icon() non accetta classi). */
function deco(name, cls = 'callout__icon') {
  const i = icon(name);
  i.classList.add(cls);
  i.setAttribute('aria-hidden', 'true');
  return i;
}

/** Icona dentro un pulsante M3 (slot icon = iniziale, trailing-icon = finale). */
function btnIcon(name, slot = 'icon') {
  const i = deco(name, 'ob-icon');
  i.slot = slot;
  return i;
}

function notice(kind, iconName, text, { role = 'status', title } = {}) {
  return h('div', { class: `callout callout--${kind}`, role },
    deco(iconName),
    h('div', {},
      title ? h('p', { class: 'callout__title' }, title) : null,
      h('p', { class: 'md-typescale-body-medium' }, text)));
}

function stepper(current) {
  const items = STEPS.map((label, i) => h('li', {
    class: `ob-stepper__item${i < current ? ' is-done' : ''}${i === current ? ' is-current' : ''}`,
    'aria-current': i === current ? 'step' : null,
  },
  h('span', { class: 'ob-stepper__dot' }, i < current ? deco('check') : String(i + 1)),
  h('span', { class: 'ob-stepper__label md-typescale-label-medium' }, label)));
  const bar = document.createElement('md-linear-progress');
  bar.className = 'ob-stepper__bar';
  bar.value = (current + 1) / STEPS.length;
  bar.setAttribute('aria-hidden', 'true');
  return h('div', { class: 'ob-stepper' },
    h('ol', { class: 'ob-stepper__list', 'aria-label': `Passo ${current + 1} di ${STEPS.length}` }, items),
    bar);
}

function art() {
  const wrap = h('div', { class: 'ob-art' });
  wrap.innerHTML = `<svg viewBox="0 0 240 160" aria-hidden="true" focusable="false">
    <rect class="ob-art__bg" width="240" height="160" rx="24"/>
    <rect class="ob-art__card ob-art__card--back" x="62" y="28" width="116" height="84" rx="14" transform="rotate(8 120 70)"/>
    <rect class="ob-art__card ob-art__card--mid" x="54" y="36" width="116" height="84" rx="14" transform="rotate(-4 112 78)"/>
    <rect class="ob-art__card ob-art__card--front" x="60" y="40" width="120" height="88" rx="14"/>
    <circle class="ob-art__sun" cx="92" cy="72" r="10"/>
    <path class="ob-art__hill" d="M68 124 L102 88 L124 110 L142 94 L174 124 Z"/>
  </svg>`;
  return wrap;
}

export async function mount(root, { navigate }) {
  loadCss();
  let alive = true;
  let srcTimer = 0;
  let dstTimer = 0;
  let srcSeq = 0;
  let dstSeq = 0;
  root.classList.add('ob-host');

  const close = () => {
    alive = false;
    clearTimeout(srcTimer);
    clearTimeout(dstTimer);
    root.classList.remove('ob-host');
  };
  const leave = () => { close(); navigate('/'); };

  const frame = h('div', { class: 'ob', role: 'region', 'aria-label': 'Benvenuto in FotoScan' });
  root.append(frame);
  frame.append(h('div', { class: 'ob__main' }, h('div', { class: 'ob__inner' },
    h('div', { class: 'skeleton skeleton-line', style: { height: '40px', width: '60%' } }),
    h('div', { class: 'skeleton skeleton-card' }))));

  let status;
  let settings;
  try {
    [status, settings] = await Promise.all([api.get('/status'), api.get('/settings')]);
  } catch (e) {
    if (!alive) return () => {};
    frame.replaceChildren(h('div', { class: 'ob__main' }, h('div', { class: 'ob__inner' },
      notice('error', 'error', e.message || 'Server non raggiungibile.', { role: 'alert', title: 'Non riesco a leggere lo stato' }),
      h('div', {}, h('md-filled-button', { onclick: () => location.reload() }, 'Riprova')))));
    return close;
  }

  const wiz = {
    step: 0,
    reduced: (status.counts?.indicizzati || 0) > 0,
    indexed: status.counts?.indicizzati || 0,
    source: settings.source_dir || '',
    output: settings.output_dir || '',
    src: { state: 'empty' }, // empty | checking | ok | missing | error
    dst: { state: 'empty' }, // empty | checking | ok | missing | inside | error
    scope: 'quick',
    env: null,
    srcField: null,
    dstField: null,
    dstHelp: null,
    modeLine: null,
    hint: null,
  };
  let dir = 1; // verso dell'ultima transizione (1 avanti, -1 indietro)
  let nextBtn = null;

  // ---------------------------------------------------------- verifiche cartelle
  const probe = (path) => api.get('/system/disk', { path });

  /** Applica lo stato al campo sorgente (senza ridisegnare: il focus resta nel campo). */
  function paintSource() {
    if (!wiz.srcField) return;
    const s = wiz.src;
    const f = wiz.srcField;
    const view = {
      empty: () => ({ error: false, text: 'Incolla il percorso oppure usa Sfoglia.' }),
      checking: () => ({ error: false, text: 'Controllo la cartella...' }),
      ok: () => ({ error: false, text: `Cartella trovata (disco ${s.disk?.drive || ''}).` }),
      missing: () => ({ error: true, errorText: 'Questa cartella non esiste. Controlla il percorso.' }),
      error: () => ({ error: true, errorText: s.msg || 'Controllo non riuscito.' }),
    }[s.state]();
    f.error = view.error;
    f.errorText = view.errorText || '';
    f.supportingText = view.text || '';
    paintMode();
    updateNext();
  }

  function destMessages() {
    const d = wiz.dst;
    if (d.state === 'inside') {
      return [notice('error', 'error', 'Le cartelle ordinate non possono stare tra le foto originali. Scegli un\'altra cartella.', { role: 'alert', title: 'Cartella non valida' })];
    }
    if (d.state !== 'ok') return [];
    const out = [];
    if (d.disk.free_bytes != null && d.disk.free_bytes < LOW_SPACE) {
      out.push(notice('warning', 'warning', `Restano circa ${fmtSize(d.disk.free_bytes)} liberi. Se puoi, libera spazio prima di continuare.`, { title: 'Poco spazio libero' }));
    }
    return out;
  }

  /** Testo informativo: Collegamento o Copia, decisi in automatico dai dischi (nessuna scelta dell'utente). */
  function modeText() {
    if (wiz.src.state !== 'ok' || wiz.dst.state !== 'ok') {
      return 'Quando scegli entrambe le cartelle, ti diciamo come verranno create le cartelle ordinate.';
    }
    return sameVolume()
      ? 'FotoScan userà: Collegamento (hard link), perché cartelle e foto sono sullo stesso disco. Le foto non occupano spazio in più.'
      : 'FotoScan userà: Copia, perché le cartelle ordinate sono su un altro disco. Le foto occuperanno altro spazio.';
  }

  function paintMode() {
    if (wiz.modeLine) wiz.modeLine.textContent = modeText();
  }

  /** Solo nel passo unico: spiega perché Salva è disattivato. */
  function paintHint() {
    if (!wiz.hint || !nextBtn) return;
    wiz.hint.textContent = nextBtn.disabled ? 'Per salvare servono due cartelle valide: quella delle foto e quella per le cartelle ordinate.' : '';
  }

  function paintDest() {
    if (!wiz.dstField) return;
    const d = wiz.dst;
    const f = wiz.dstField;
    const view = {
      empty: () => ({ error: false, text: 'Scegli una cartella diversa da quella delle foto.' }),
      inside: () => ({ error: true, errorText: 'Non può stare dentro la cartella delle foto.' }),
      checking: () => ({ error: false, text: 'Controllo la cartella...' }),
      ok: () => ({ error: false, text: `Cartella trovata (disco ${d.disk?.drive || ''}).` }),
      missing: () => ({ error: true, errorText: 'Questa cartella non esiste. Creala in Esplora risorse e riprova.' }),
      error: () => ({ error: true, errorText: d.msg || 'Controllo non riuscito.' }),
    }[d.state]();
    f.error = view.error;
    f.errorText = view.errorText || '';
    f.supportingText = view.text || '';
    if (wiz.dstHelp) wiz.dstHelp.replaceChildren(...destMessages());
    paintMode();
    updateNext();
  }

  /** Vero se destinazione e foto stanno sullo stesso disco (lettera di unita' uguale). */
  function sameVolume() {
    return wiz.src.state === 'ok' && wiz.dst.state === 'ok' && sameDrive(wiz.src.disk?.drive, wiz.dst.disk?.drive);
  }

  function scheduleSource() {
    clearTimeout(srcTimer);
    srcTimer = setTimeout(checkSource, 350);
  }
  function scheduleDest() {
    clearTimeout(dstTimer);
    dstTimer = setTimeout(checkDest, 350);
  }

  async function checkSource() {
    const my = ++srcSeq;
    const v = wiz.source.trim();
    if (!v) {
      wiz.src = { state: 'empty' };
    } else {
      wiz.src = { state: 'checking' };
      paintSource();
      try {
        const d = await probe(v);
        if (!alive || my !== srcSeq) return;
        wiz.src = d.exists ? { state: 'ok', disk: d } : { state: 'missing' };
      } catch (e) {
        if (!alive || my !== srcSeq) return;
        wiz.src = { state: 'error', msg: e.message };
      }
    }
    paintSource();
    scheduleDest(); // la destinazione dipende dalla sorgente (dentro/fuori, stesso disco)
  }

  async function checkDest() {
    const my = ++dstSeq;
    const v = wiz.output.trim();
    if (!v) {
      wiz.dst = { state: 'empty' };
    } else if (wiz.source.trim() && isInside(v, wiz.source)) {
      wiz.dst = { state: 'inside' };
    } else {
      wiz.dst = { state: 'checking' };
      paintDest();
      try {
        const d = await probe(v);
        if (!alive || my !== dstSeq) return;
        wiz.dst = d.exists ? { state: 'ok', disk: d } : { state: 'missing' };
      } catch (e) {
        if (!alive || my !== dstSeq) return;
        wiz.dst = { state: 'error', msg: e.message };
      }
    }
    paintDest();
  }

  function updateNext() {
    if (!nextBtn) return;
    if (wiz.reduced) {
      nextBtn.disabled = !(wiz.src.state === 'ok' && wiz.dst.state === 'ok');
      paintHint();
      return;
    }
    const ready = [
      () => true,
      () => wiz.src.state === 'ok',
      () => wiz.dst.state === 'ok',
      () => true,
    ][wiz.step]();
    nextBtn.disabled = !ready;
  }

  /** Sfoglia: finestra nativa; path null = annullato (nessuna modifica). */
  function pickFor(which, title) {
    return async (btn) => {
      btn.disabled = true;
      try {
        const current = which === 'source' ? wiz.source : wiz.output;
        const r = await api.post('/system/pick-folder', { title, initial: current.trim() || undefined });
        if (!r || !r.path || !alive) return;
        if (which === 'source') {
          wiz.source = r.path;
          wiz.srcField.value = r.path;
          checkSource();
        } else {
          wiz.output = r.path;
          wiz.dstField.value = r.path;
          checkDest();
        }
      } catch (e) {
        if (alive) showSnackbar(e.status === 409 ? 'Una finestra di scelta è già aperta.' : e.message, { error: true });
      } finally {
        btn.disabled = false;
      }
    };
  }

  /** Campo cartella: testo + Sfoglia. Il riferimento al campo è salvato in wiz per gli aggiornamenti. */
  function folderRow({ which, label, pickTitle, onEnter }) {
    const field = document.createElement('md-outlined-text-field');
    field.label = label;
    field.value = which === 'source' ? wiz.source : wiz.output;
    field.className = 'ob-field__input';
    field.addEventListener('input', () => {
      // Il valore cambia: la verifica precedente non vale più. Avanti/Salva si bloccano subito.
      if (which === 'source') { wiz.source = field.value; wiz.src = { state: 'checking' }; paintSource(); scheduleSource(); }
      else { wiz.output = field.value; wiz.dst = { state: 'checking' }; paintDest(); scheduleDest(); }
    });
    field.addEventListener('keydown', (e) => { if (e.key === 'Enter' && onEnter) onEnter(); });
    const pick = h('md-outlined-button', { class: 'ob-field__pick', onclick: () => pickFor(which, pickTitle)(pick) },
      btnIcon('folder_open'), 'Sfoglia');
    if (which === 'source') wiz.srcField = field; else wiz.dstField = field;
    return h('div', { class: 'ob-field' }, h('div', { class: 'ob-field__row' }, field, pick));
  }

  /** Invio in un campo: nel passo unico salva (se valido); nel percorso guidato va avanti (next controlla già lo stato). */
  const enterKey = () => {
    if (wiz.reduced) { if (nextBtn && !nextBtn.disabled) save(); }
    else next();
  };

  const sourceField = () => folderRow({
    which: 'source',
    label: 'Cartella delle foto',
    pickTitle: 'Scegli la cartella delle tue foto',
    onEnter: enterKey,
  });

  function destField() {
    wiz.dstHelp = h('div', { class: 'ob-help', 'aria-live': 'polite' });
    wiz.modeLine = h('p', { class: 'md-typescale-body-medium ob-muted ob-mode-line' });
    return [
      folderRow({
        which: 'output',
        label: 'Cartella per le foto ordinate',
        pickTitle: 'Scegli dove creare le cartelle ordinate',
        onEnter: enterKey,
      }),
      wiz.dstHelp,
      wiz.modeLine,
    ];
  }

  // ---------------------------------------------------------- contenuto dei passi
  const intro = (title, lead) => h('div', { class: 'ob-intro' },
    h('h2', { class: 'md-typescale-headline-medium ob-title', tabindex: '-1' }, title),
    h('p', { class: 'md-typescale-body-large ob-lead' }, lead));

  function welcomeStep() {
    const points = [
      ['lock', 'Le tue foto restano sul tuo computer', 'Nessun servizio esterno. Serve internet solo una volta, per scaricare il modello di riconoscimento.'],
      ['shield', 'Non cancelliamo e non spostiamo i tuoi originali', 'Le foto originali restano dove sono, identiche.'],
      ['visibility', 'Prima vedi l\'anteprima, poi decidi', 'Ti mostriamo il piano delle cartelle e aspettiamo il tuo ok.'],
    ];
    return [
      art(),
      intro('Benvenuto! Ritroviamo i tuoi ricordi', 'In pochi passi colleghiamo le tue foto e prepariamo l\'ordine. Ci vogliono circa due minuti.'),
      h('ul', { class: 'ob-points' }, points.map(([ic, t, d]) => h('li', { class: 'ob-point' },
        h('span', { class: 'ob-point__icon' }, deco(ic)),
        h('span', { class: 'ob-point__text' },
          h('span', { class: 'md-typescale-title-medium' }, t),
          h('span', { class: 'md-typescale-body-medium ob-muted' }, d))))),
    ];
  }

  function sourceStep() {
    return [
      intro('Dove sono le tue foto?', 'Scegli la cartella che contiene foto e video, anche con le sottocartelle. La leggiamo soltanto: non la modifichiamo.'),
      sourceField(),
    ];
  }

  function destStep() {
    return [
      intro('Dove creare le cartelle ordinate?', 'Qui creeremo le cartelle ordinate per anno, mese ed evento. Usa una cartella diversa da quella delle foto.'),
      ...destField(),
    ];
  }

  function checksStep() {
    const checks = h('div', { class: 'ob-checks' },
      h('div', { class: 'skeleton skeleton-line', style: { height: '64px' } }));
    const choices = [
      { value: 'quick', label: `Prova veloce (${QUICK_LIMIT} file, circa 1 minuto)`, desc: 'Vedi subito il risultato. Poi potrai indicizzare tutto.' },
      { value: 'all', label: 'Indicizza tutto', desc: 'Analizza tutte le foto e i video. Può richiedere molto tempo: puoi fermarti e riprendere quando vuoi.' },
    ];
    const group = h('div', { class: 'ob-choices', role: 'radiogroup', 'aria-label': 'Quanti file analizzare' },
      choices.map((c) => {
        const radio = document.createElement('md-radio');
        radio.name = 'scope';
        radio.value = c.value;
        radio.checked = wiz.scope === c.value;
        radio.addEventListener('change', () => { if (radio.checked) wiz.scope = c.value; });
        return h('label', { class: 'ob-choice' }, radio,
          h('span', { class: 'ob-choice__text' },
            h('span', { class: 'md-typescale-title-small' }, c.label),
            h('span', { class: 'md-typescale-body-medium ob-muted' }, c.desc)));
      }));
    loadEnvironment(checks);
    return [
      intro('Controllo e avvio', 'Ecco come è messo il tuo computer. Puoi iniziare anche senza cambiare nulla.'),
      h('div', { class: 'ob-summary' },
        h('p', { class: 'md-typescale-label-large ob-muted' }, 'Cartelle'),
        h('p', { class: 'md-typescale-body-medium ob-path' }, `Foto: ${wiz.source.trim() || '-'}`),
        h('p', { class: 'md-typescale-body-medium ob-path' }, `Ordinate: ${wiz.output.trim() || '-'}`)),
      checks,
      h('div', { class: 'ob-modes' },
        h('h3', { class: 'md-typescale-title-medium' }, 'Quanto analizzare adesso?'),
        group),
    ];
  }

  /** Controlli di ambiente da GET /status: dispositivo, FFmpeg, modello. */
  async function loadEnvironment(host) {
    let st;
    try {
      st = await api.get('/status');
    } catch (e) {
      if (alive) host.replaceChildren(notice('error', 'error', e.message, { role: 'alert', title: 'Controllo non riuscito' }));
      return;
    }
    if (!alive) return;
    const gpu = st.device === 'cuda' || st.device === 'mps';
    const rows = [
      gpu
        ? { kind: 'ok', ic: 'memory', title: 'Elaborazione veloce', text: `Scheda grafica disponibile (${String(st.device).toUpperCase()}).` }
        : { kind: 'info', ic: 'memory', title: 'Elaborazione sul processore', text: 'Funziona, ma l\'analisi delle foto sarà più lenta.' },
      st.ffmpeg
        ? { kind: 'ok', ic: 'movie', title: 'Video', text: 'FFmpeg trovato: le anteprime dei video sono disponibili.' }
        : { kind: 'warning', ic: 'movie', title: 'Video', ffmpeg: true },
      st.model_cached
        ? { kind: 'ok', ic: 'psychology', title: 'Modello di riconoscimento', text: 'Pronto, nessun download necessario.' }
        : { kind: 'info', ic: 'cloud_download', title: 'Modello di riconoscimento', text: 'Verrà scaricato una sola volta al primo avvio: serve una connessione internet.' },
    ];
    host.replaceChildren(...rows.map((r) => h('div', { class: `ob-check ob-check--${r.kind}` },
      h('span', { class: 'ob-check__icon' }, deco(r.kind === 'ok' ? 'check_circle' : r.kind === 'warning' ? 'warning' : 'info')),
      h('div', { class: 'ob-check__body' },
        h('p', { class: 'md-typescale-title-small' }, r.title),
        r.ffmpeg
          ? [
            h('p', { class: 'md-typescale-body-medium ob-muted' },
              'FFmpeg non trovato: le anteprime dei video non saranno disponibili. Per installarlo, apri il Prompt dei comandi e digita:'),
            h('code', { class: 'ob-code' }, 'winget install Gyan.FFmpeg'),
            h('p', { class: 'md-typescale-body-medium ob-muted' }, 'Poi riavvia FotoScan.'),
          ]
          : h('p', { class: 'md-typescale-body-medium ob-muted' }, r.text)))));
  }

  function reducedStep() {
    wiz.hint = h('p', { class: 'ob-hint md-typescale-body-medium', 'aria-live': 'polite' });
    return [
      intro('Vuoi cambiare cartelle?', `Il tuo archivio è già pronto con ${formatNumber(wiz.indexed)} file indicizzati. Le modifiche valgono dalla prossima indicizzazione.`),
      sourceField(),
      ...destField(),
      wiz.hint,
    ];
  }

  // ---------------------------------------------------------- navigazione
  /** Il passo corrente è completo? (il passo 0 e 3 sempre; 1 serve la sorgente; 2 la destinazione). */
  async function stepReady() {
    if (wiz.step === 1 && wiz.src.state === 'empty' && wiz.source.trim()) await checkSource();
    if (wiz.step === 2 && wiz.dst.state === 'empty' && wiz.output.trim()) await checkDest();
    if (wiz.step === 1) return wiz.src.state === 'ok';
    if (wiz.step === 2) return wiz.dst.state === 'ok';
    return true;
  }

  function next() {
    if (nextBtn && nextBtn.disabled) return;
    go(1);
  }

  async function go(delta) {
    if (delta > 0 && !(await stepReady())) return;
    dir = delta;
    wiz.step = Math.max(0, Math.min(STEPS.length - 1, wiz.step + delta));
    render();
  }

  function render() {
    wiz.srcField = null;
    wiz.dstField = null;
    wiz.dstHelp = null;
    wiz.modeLine = null;
    wiz.hint = null;
    const body = wiz.reduced ? reducedStep() : [welcomeStep, sourceStep, destStep, checksStep][wiz.step]();
    const panel = h('section', { class: `ob-panel ${dir >= 0 ? 'is-fwd' : 'is-back'}` }, body);
    const main = h('div', { class: 'ob__main' }, h('div', { class: 'ob__inner' },
      wiz.reduced ? null : stepper(wiz.step),
      panel));
    const top = h('header', { class: 'ob__top' }, h('div', { class: 'ob__col' },
      h('span', { class: 'ob__brand' },
        h('img', { class: 'ob__brand-logo', src: 'favicon.svg', alt: '', width: 28, height: 28 }),
        h('span', { class: 'md-typescale-title-large' }, 'FotoScan')),
      h('span', { class: 'spacer' }),
      wiz.reduced ? null : h('md-text-button', { onclick: leave }, 'Salta per ora')));
    frame.replaceChildren(top, main, buildFooter());
    // Il ridisegno ricrea i campi: riapplica lo stato già noto.
    paintSource();
    paintDest();
    updateNext();
    requestAnimationFrame(() => {
      const t = frame.querySelector('.ob-title');
      if (t) t.focus({ preventScroll: true });
    });
  }

  function buildFooter() {
    // Stessa colonna del contenuto (.ob__col), così i pulsanti non stanno agli estremi dello schermo.
    if (wiz.reduced) {
      nextBtn = h('md-filled-button', { onclick: save }, 'Salva');
      return h('footer', { class: 'ob__foot' }, h('div', { class: 'ob__col' },
        h('md-text-button', { onclick: leave }, 'Annulla'),
        h('span', { class: 'spacer' }),
        nextBtn));
    }
    const last = wiz.step === STEPS.length - 1;
    const back = wiz.step > 0
      ? h('md-outlined-button', { onclick: () => go(-1) }, btnIcon('arrow_back'), 'Indietro')
      : null;
    nextBtn = last
      ? h('md-filled-button', { onclick: start }, btnIcon('play_arrow'), 'Inizia')
      : h('md-filled-button', { onclick: () => go(1) }, 'Avanti', btnIcon('arrow_forward', 'trailing-icon'));
    return h('footer', { class: 'ob__foot' }, h('div', { class: 'ob__col' },
      back, h('span', { class: 'spacer' }), nextBtn));
  }

  function settingsPayload() {
    return {
      source_dir: wiz.source.trim(),
      output_dir: wiz.output.trim(),
      link_mode: sameVolume() ? 'hardlink' : 'copy',
    };
  }

  async function save() {
    nextBtn.disabled = true;
    try {
      await api.put('/settings', settingsPayload());
      showSnackbar('Cartelle aggiornate.');
      leave();
    } catch (e) {
      if (alive) showSnackbar(e.message, { error: true });
      nextBtn.disabled = false;
    }
  }

  async function start() {
    nextBtn.disabled = true;
    try {
      await api.put('/settings', settingsPayload());
      await api.post('/index/start', wiz.scope === 'quick' ? { limit: QUICK_LIMIT } : {});
    } catch (e) {
      if (e.status !== 409) {
        if (alive) showSnackbar(e.message, { error: true });
        if (alive) nextBtn.disabled = false;
        return;
      }
      showSnackbar('L\'indicizzazione è già in corso.');
    }
    try { store.set('status', await api.get('/status')); } catch { /* non bloccante */ }
    close();
    navigate('/index');
  }

  // ---------------------------------------------------------- avvio
  render();
  if (wiz.source.trim()) checkSource();
  if (wiz.output.trim()) checkDest();
  return close;
}
