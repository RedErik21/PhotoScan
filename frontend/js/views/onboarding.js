// Prima configurazione a schermo intero: 3 passi (Benvenuto, Cartella foto, Controllo e avvio)
// oppure vista rapida per cambiare la cartella se l'archivio è già indicizzato.
// Copre la shell con l'overlay .ob (css/view-onboarding.css).
import { h, icon } from '../ui/el.js';
import { createWordmark } from '../ui/brand.js';
import { api } from '../api.js';
import { store, applyTheme } from '../store.js';
import { showSnackbar } from '../ui/snackbar.js';
import { formatBytes, formatNumber } from '../ui/format.js';

const CSS_HREF = new URL('../../css/view-onboarding.css', import.meta.url).href;
const STEPS = ['Benvenuto', 'Cartella foto', 'Controllo e avvio'];
const QUICK_LIMIT = 300;
const THEME_ICON = { system: 'contrast', light: 'light_mode', dark: 'dark_mode' };
const THEME_NEXT = { system: 'light', light: 'dark', dark: 'system' };
const THEME_LABEL = { system: 'Tema di sistema', light: 'Tema chiaro', dark: 'Tema scuro' };

function loadCss() {
  if (document.querySelector('link[data-view-css="onboarding"]')) return;
  if ([...document.querySelectorAll('link[rel="stylesheet"]')].some((l) => l.href === CSS_HREF)) return;
  const link = document.createElement('link');
  link.rel = 'stylesheet';
  link.href = CSS_HREF;
  link.dataset.viewCss = 'onboarding';
  document.head.append(link);
}

function notice(kind, iconName, text, { role = 'status', title } = {}) {
  const ic = icon(iconName);
  ic.classList.add('callout__icon');
  ic.setAttribute('aria-hidden', 'true');
  return h('div', { class: `callout callout--${kind}`, role },
    ic,
    h('div', {},
      title ? h('p', { class: 'callout__title' }, title) : null,
      h('p', { class: 'md-typescale-body-medium' }, text)));
}

/** Illustrazioni SVG minimali per la colonna brand, coerenti con il passo corrente. */
function asideArt(step) {
  const wrap = h('div', { class: 'ob-aside__art', 'aria-hidden': 'true' });
  if (step === 1) {
    wrap.innerHTML = `<svg viewBox="0 0 220 140" fill="none">
      <rect x="24" y="34" width="172" height="86" rx="16" fill="rgba(255,255,255,0.14)" stroke="rgba(255,255,255,0.34)" stroke-width="1.5"/>
      <path d="M24 48 C24 39 31 32 40 32 L86 32 L98 44 L180 44 C189 44 196 51 196 60 L196 104 C196 113 189 120 180 120 L40 120 C31 120 24 113 24 104 Z" fill="rgba(255,255,255,0.18)" stroke="rgba(255,255,255,0.42)" stroke-width="1.5"/>
      <rect x="48" y="58" width="46" height="44" rx="8" fill="rgba(255,255,255,0.92)" transform="rotate(-6 71 80)"/>
      <circle cx="64" cy="72" r="5" fill="#e0742f"/>
      <path d="M52 96 L66 80 L76 89 L84 82 L94 96 Z" fill="#4b45d8"/>
      <rect x="108" y="64" width="64" height="8" rx="4" fill="rgba(255,255,255,0.75)"/>
      <rect x="108" y="80" width="48" height="6" rx="3" fill="rgba(255,255,255,0.45)"/>
      <rect x="108" y="94" width="36" height="6" rx="3" fill="rgba(255,255,255,0.32)"/>
    </svg>`;
  } else if (step === 2) {
    wrap.innerHTML = `<svg viewBox="0 0 220 140" fill="none">
      <rect x="28" y="22" width="164" height="96" rx="16" fill="rgba(255,255,255,0.14)" stroke="rgba(255,255,255,0.34)" stroke-width="1.5"/>
      <circle cx="56" cy="50" r="10" fill="#6fdc9a"/>
      <path d="M52 50 L55 53 L61 46" stroke="#0b3a1f" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"/>
      <rect x="76" y="45" width="92" height="9" rx="4.5" fill="rgba(255,255,255,0.85)"/>
      <circle cx="56" cy="78" r="10" fill="#6fdc9a"/>
      <path d="M52 78 L55 81 L61 74" stroke="#0b3a1f" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"/>
      <rect x="76" y="73" width="76" height="9" rx="4.5" fill="rgba(255,255,255,0.68)"/>
      <rect x="46" y="100" width="128" height="6" rx="3" fill="rgba(255,255,255,0.22)"/>
      <rect x="46" y="100" width="92" height="6" rx="3" fill="#ffffff"/>
    </svg>`;
  } else {
    wrap.innerHTML = `<svg viewBox="0 0 220 140" fill="none">
      <rect x="58" y="18" width="104" height="78" rx="14" fill="rgba(255,255,255,0.22)" transform="rotate(9 110 57)"/>
      <rect x="48" y="26" width="104" height="78" rx="14" fill="rgba(255,255,255,0.34)" transform="rotate(-6 100 65)"/>
      <rect x="54" y="30" width="112" height="84" rx="15" fill="rgba(255,255,255,0.96)"/>
      <circle cx="84" cy="58" r="10" fill="#e0742f"/>
      <path d="M62 106 L94 72 L116 92 L134 76 L158 106 Z" fill="#4b45d8"/>
      <circle cx="164" cy="34" r="14" fill="#ffe2d1"/>
      <path d="M164 26 L166.4 31.6 L172 34 L166.4 36.4 L164 42 L161.6 36.4 L156 34 L161.6 31.6 Z" fill="#9a4b1e"/>
    </svg>`;
  }
  return wrap;
}

const ASIDE_COPY = [
  {
    quote: 'I tuoi ricordi, organizzati e ricercabili in locale.',
    sub: 'Cerca foto descrivendole in italiano, esplora luoghi e persone, libera spazio in sicurezza.',
  },
  {
    quote: 'Accesso in sola lettura alla tua libreria.',
    sub: 'PhotoScan indicizza foto e video in un catalogo separato: i tuoi file originali restano intatti.',
  },
  {
    quote: 'Pronto per la prima analisi.',
    sub: 'Puoi provare subito con 300 file oppure indicizzare l’intero archivio in background.',
  },
];

export async function mount(root, { navigate, signal }) {
  loadCss();
  let alive = true;
  let srcTimer = 0;
  let srcSeq = 0;
  let hwLoaded = false;
  root.classList.add('ob-host');

  const unsubs = [];
  const close = () => {
    alive = false;
    clearTimeout(srcTimer);
    unsubs.forEach((off) => off());
    unsubs.length = 0;
    root.classList.remove('ob-host');
  };
  const leave = () => { close(); navigate('/'); };

  const frame = h('div', { class: 'ob', role: 'region', 'aria-label': 'Configurazione iniziale di PhotoScan' });
  const stage = h('div', { class: 'ob-stage' });
  frame.append(stage);
  root.append(frame);

  // Skeleton iniziale mentre carichiamo /status e /settings
  stage.append(
    h('div', { class: 'ob-workspace', style: { padding: 'var(--sp-8)', gap: 'var(--sp-5)' } },
      h('div', { class: 'skeleton skeleton-line', style: { height: '32px', width: '48%' } }),
      h('div', { class: 'skeleton skeleton-card', style: { minHeight: '220px' } }),
      h('div', { class: 'skeleton skeleton-line', style: { height: '18px', width: '70%' } })));

  let status;
  let settings;
  try {
    [status, settings] = await Promise.all([
      api.get('/status', undefined, signal),
      api.get('/settings', undefined, signal),
    ]);
  } catch (e) {
    if (!alive || e?.name === 'AbortError') return close;
    stage.replaceChildren(
      h('div', { class: 'ob-workspace', style: { padding: 'var(--sp-8)', justifyContent: 'center', gap: 'var(--sp-4)' } },
        notice('error', 'error', e.message || 'Server locale non raggiungibile.', {
          role: 'alert',
          title: 'Impossibile leggere lo stato di PhotoScan',
        }),
        h('div', {}, h('md-filled-button', { onclick: () => location.reload() }, icon('refresh', { slot: 'icon' }), 'Riprova'))));
    return close;
  }
  if (!alive) return close;

  const wiz = {
    step: 0,
    reduced: (status.counts?.indicizzati || 0) > 0,
    indexed: status.counts?.indicizzati || 0,
    source: settings.source_dir || '',
    enableFaces: settings.enable_faces !== false,
    facesAvailable: status.faces?.available !== false,
    src: { state: 'empty', disk: null, msg: '' }, // empty | checking | ok | is_file | missing | error
    scope: 'quick',
    status,
    hw: null,
    srcField: null,
    folderBadge: null,
    hint: null,
  };
  let dir = 1;
  let nextBtn = null;
  let themeBtn = null;

  // ---------------------------------------------------------- tema
  function paintThemeBtn() {
    if (!themeBtn) return;
    const mode = store.get('theme', 'system');
    themeBtn.replaceChildren(icon(THEME_ICON[mode] || 'contrast'));
    themeBtn.title = `${THEME_LABEL[mode] || 'Tema'} (clicca per cambiare)`;
    themeBtn.setAttribute('aria-label', `${THEME_LABEL[mode] || 'Tema'}: cambia tema`);
  }
  unsubs.push(store.subscribe('theme', paintThemeBtn));

  // ---------------------------------------------------------- verifica cartella
  function paintSource() {
    if (!wiz.srcField) return;
    const s = wiz.src;
    const f = wiz.srcField;
    const view = {
      empty: () => ({ error: false, text: 'Incolla il percorso della cartella oppure usa Sfoglia.' }),
      checking: () => ({ error: false, text: 'Controllo la cartella…' }),
      ok: () => ({ error: false, text: 'Cartella valida e accessibile in lettura.' }),
      is_file: () => ({ error: true, errorText: 'Questo è un file, non una cartella. Scegli la cartella che contiene le foto.' }),
      missing: () => ({ error: true, errorText: 'Questa cartella non esiste. Controlla il percorso e riprova.' }),
      error: () => ({ error: true, errorText: s.msg || 'Impossibile verificare questa cartella.' }),
    }[s.state]();

    f.error = view.error;
    f.errorText = view.errorText || '';
    f.supportingText = view.error ? '' : (view.text || '');

    if (wiz.folderBadge) {
      if (s.state === 'ok' && s.disk) {
        const parts = [`Cartella trovata`];
        if (s.disk.drive) parts.push(`unità ${s.disk.drive}`);
        if (s.disk.free_bytes != null) parts.push(`${formatBytes(s.disk.free_bytes)} liberi`);
        wiz.folderBadge.hidden = false;
        wiz.folderBadge.replaceChildren(
          icon('check_circle'),
          h('span', {}, parts.join(' · ')));
      } else {
        wiz.folderBadge.hidden = true;
      }
    }
    updateNext();
  }

  function paintHint() {
    if (!wiz.hint || !nextBtn) return;
    wiz.hint.textContent = nextBtn.disabled
      ? 'Indica una cartella esistente per salvare la modifica.'
      : '';
  }

  function scheduleSource() {
    clearTimeout(srcTimer);
    srcTimer = setTimeout(checkSource, 350);
  }

  async function checkSource() {
    const my = ++srcSeq;
    const v = wiz.source.trim();
    if (!v) {
      wiz.src = { state: 'empty', disk: null, msg: '' };
    } else {
      wiz.src = { state: 'checking', disk: null, msg: '' };
      paintSource();
      try {
        const d = await api.get('/system/disk', { path: v });
        if (!alive || my !== srcSeq) return;
        if (d.is_file) wiz.src = { state: 'is_file', disk: d, msg: '' };
        else if (d.exists) wiz.src = { state: 'ok', disk: d, msg: '' };
        else wiz.src = { state: 'missing', disk: d, msg: '' };
      } catch (e) {
        if (!alive || my !== srcSeq) return;
        wiz.src = { state: 'error', disk: null, msg: e.message };
      }
    }
    paintSource();
  }

  function updateNext() {
    if (!nextBtn) return;
    if (wiz.reduced) {
      nextBtn.disabled = wiz.src.state !== 'ok';
      paintHint();
      return;
    }
    const ready = wiz.step === 1 ? wiz.src.state === 'ok' : true;
    nextBtn.disabled = !ready;
  }

  async function pickFolder(btn, btnLabel) {
    btn.disabled = true;
    btn.setAttribute('aria-busy', 'true');
    btnLabel.textContent = 'Apro…';
    try {
      const r = await api.post('/system/pick-folder', {
        title: 'Scegli la cartella delle tue foto',
        initial: wiz.source.trim() || undefined,
      });
      if (!alive) return;
      if (r && r.available === false) {
        showSnackbar('La finestra di scelta cartella non è disponibile: incolla il percorso nel campo.', { error: true });
        return;
      }
      if (!r || !r.path) return;
      wiz.source = r.path;
      if (wiz.srcField) wiz.srcField.value = r.path;
      checkSource();
    } catch (e) {
      if (alive) {
        showSnackbar(e.status === 409 ? 'Una finestra di scelta è già aperta.' : e.message, { error: true });
      }
    } finally {
      btn.disabled = false;
      btn.removeAttribute('aria-busy');
      btnLabel.textContent = 'Sfoglia…';
    }
  }

  const enterKey = () => {
    if (wiz.reduced) {
      if (nextBtn && !nextBtn.disabled) saveReduced();
    } else if (nextBtn && !nextBtn.disabled) {
      go(1);
    }
  };

  function sourceFieldBlock() {
    const field = document.createElement('md-outlined-text-field');
    field.label = 'Cartella delle foto';
    field.value = wiz.source;
    field.className = 'ob-field__input';
    field.placeholder = 'Es. C:\\Users\\Nome\\Pictures';
    field.addEventListener('input', () => {
      wiz.source = field.value;
      wiz.src = field.value.trim() ? { state: 'checking', disk: null, msg: '' } : { state: 'empty', disk: null, msg: '' };
      paintSource();
      scheduleSource();
    });
    field.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') enterKey();
    });

    const btnLabel = document.createTextNode('Sfoglia…');
    const pick = h('md-outlined-button', {
      class: 'ob-field__pick',
      onclick: () => pickFolder(pick, btnLabel),
    }, icon('folder_open', { slot: 'icon' }), btnLabel);

    wiz.srcField = field;
    wiz.folderBadge = h('div', { class: 'ob-folder-status', role: 'status', 'aria-live': 'polite', hidden: true });

    return h('div', { class: 'ob-field' },
      h('div', { class: 'ob-field__row' }, field, pick),
      wiz.folderBadge);
  }

  // ---------------------------------------------------------- passi
  const intro = (title, lead) => h('div', { class: 'ob-intro' },
    h('h2', { class: 'ob-title', tabindex: '-1' }, title),
    h('p', { class: 'ob-lead' }, lead));

  function welcomeStep() {
    const points = [
      {
        ic: 'lock',
        tone: '',
        title: '100% locale e privato',
        desc: 'Le tue foto restano sul tuo computer, senza cloud né account. Internet serve solo una volta per scaricare il modello AI.',
      },
      {
        ic: 'shield',
        tone: 'ob-point--safe',
        title: 'Originali sempre intatti',
        desc: 'PhotoScan legge la tua cartella senza mai modificare, spostare o cancellare file senza il tuo consenso.',
      },
      {
        ic: 'auto_awesome',
        tone: 'ob-point--warm',
        title: 'Ricerca, volti, luoghi e pulizia',
        desc: 'Cerca foto descrivendole in italiano, esplora persone e mappa, e libera spazio da duplicati e video pesanti.',
      },
    ];
    return [
      intro(
        'Benvenuto in PhotoScan',
        'In pochi passi colleghiamo la tua libreria fotografica e prepariamo l’analisi locale. Ci vogliono circa due minuti.',
      ),
      h('ul', { class: 'ob-points' }, points.map((p) => h('li', { class: `ob-point ${p.tone}`.trim() },
        h('span', { class: 'ob-point__icon', 'aria-hidden': 'true' }, icon(p.ic)),
        h('div', { class: 'ob-point__text' },
          h('span', { class: 'ob-point__title' }, p.title),
          h('span', { class: 'ob-point__desc' }, p.desc))))),
    ];
  }

  function sourceStep() {
    return [
      intro(
        'Dove sono le tue foto?',
        'Scegli la cartella principale che contiene foto e video: includeremo automaticamente tutte le sottocartelle.',
      ),
      sourceFieldBlock(),
      h('div', { class: 'ob-tip' },
        icon('info'),
        h('p', {}, 'Puoi cambiare cartella quando vuoi dalle Impostazioni, oppure importare nuove foto dal telefono Android dalla sezione Telefono.')),
    ];
  }

  function checksStep() {
    const checksHost = h('div', { class: 'ob-checks' },
      h('div', { class: 'skeleton skeleton-line', style: { height: '56px' } }),
      h('div', { class: 'skeleton skeleton-line', style: { height: '56px' } }));

    const choices = [
      {
        value: 'quick',
        title: `Prova veloce (${QUICK_LIMIT} file)`,
        badge: 'Consigliato',
        desc: 'Circa 1 minuto. Vedi subito galleria, ricerca e album; poi analizzi il resto quando vuoi.',
      },
      {
        value: 'all',
        title: 'Analizza tutto l’archivio',
        badge: null,
        desc: 'Indicizza tutte le foto e i video. Puoi mettere in pausa e riprendere in qualsiasi momento.',
      },
    ];

    const choiceNodes = [];
    const group = h('div', { class: 'ob-choices', role: 'radiogroup', 'aria-label': 'Quanti file analizzare' });
    for (const c of choices) {
      const radio = document.createElement('md-radio');
      radio.name = 'ob-scope';
      radio.value = c.value;
      radio.checked = wiz.scope === c.value;
      const card = h('label', { class: `ob-choice${wiz.scope === c.value ? ' is-selected' : ''}` },
        radio,
        h('span', { class: 'ob-choice__text' },
          h('span', { class: 'ob-choice__head' },
            h('span', { class: 'ob-choice__title' }, c.title),
            c.badge ? h('span', { class: 'ob-choice__badge' }, c.badge) : null),
          h('span', { class: 'ob-choice__desc' }, c.desc)));
      radio.addEventListener('change', () => {
        if (!radio.checked) return;
        wiz.scope = c.value;
        choiceNodes.forEach(({ el, val }) => el.classList.toggle('is-selected', val === wiz.scope));
      });
      choiceNodes.push({ el: card, val: c.value });
      group.append(card);
    }

    let facesCard = null;
    if (wiz.facesAvailable) {
      const sw = document.createElement('md-switch');
      sw.selected = wiz.enableFaces;
      sw.setAttribute('aria-label', 'Attiva riconoscimento volti');
      sw.addEventListener('change', () => { wiz.enableFaces = sw.selected; });
      facesCard = h('div', { class: 'ob-toggle-card' },
        h('span', { class: 'ob-toggle-card__icon', 'aria-hidden': 'true' }, icon('face')),
        h('div', { class: 'ob-toggle-card__text' },
          h('span', { class: 'ob-toggle-card__title' }, 'Riconoscimento volti'),
          h('span', { class: 'ob-toggle-card__desc' }, 'Raggruppa automaticamente le persone nelle foto, tutto in locale sul tuo PC.')),
        sw);
    }

    loadEnvironment(checksHost);

    return [
      intro(
        'Controllo e avvio',
        'Tutto è pronto. Verifica lo stato del tuo computer e scegli come iniziare la prima analisi.',
      ),
      h('div', { class: 'ob-summary' },
        h('span', { class: 'ob-summary__icon', 'aria-hidden': 'true' }, icon('folder')),
        h('div', { class: 'ob-summary__body' },
          h('span', { class: 'ob-summary__label' }, 'Cartella delle foto'),
          h('p', { class: 'ob-path' }, wiz.source.trim() || '—')),
        h('md-text-button', { onclick: () => goTo(1) }, 'Cambia')),
      checksHost,
      facesCard,
      h('div', { class: 'ob-modes' },
        h('h3', { class: 'ob-modes__title' }, 'Quanto analizzare adesso?'),
        group),
    ];
  }

  async function loadEnvironment(host) {
    let st = wiz.status;
    let hw = wiz.hw;
    try {
      const reqs = [api.get('/status')];
      if (!hwLoaded) reqs.push(api.get('/hardware').catch(() => null));
      const res = await Promise.all(reqs);
      if (!alive) return;
      st = res[0];
      wiz.status = st;
      if (!hwLoaded) {
        hwLoaded = true;
        hw = res[1];
        wiz.hw = hw;
      }
    } catch (e) {
      if (alive) host.replaceChildren(notice('error', 'error', e.message, { role: 'alert', title: 'Controllo non riuscito' }));
      return;
    }

    const gpu = st.device === 'cuda' || st.device === 'mps';
    const gpuName = hw?.hardware?.gpu_name;
    const cpuName = hw?.hardware?.cpu_name;

    const rows = [
      gpu
        ? {
            kind: 'ok',
            title: 'Accelerazione attiva',
            desc: gpuName
              ? `Scheda grafica rilevata: ${gpuName} (${String(st.device).toUpperCase()}).`
              : `Scheda grafica disponibile (${String(st.device).toUpperCase()}): l’analisi sarà veloce.`,
          }
        : {
            kind: 'info',
            title: 'Elaborazione sul processore',
            desc: cpuName
              ? `Userà ${cpuName}. Funziona regolarmente, ma l’analisi sarà più graduale.`
              : 'Funziona regolarmente su CPU, ma l’analisi richiederà più tempo.',
          },
      st.ffmpeg
        ? {
            kind: 'ok',
            title: 'Anteprime video pronte',
            desc: 'FFmpeg trovato: PhotoScan può leggere e mostrare le anteprime dei video.',
          }
        : {
            kind: 'warning',
            title: 'FFmpeg non trovato (opzionale)',
            ffmpeg: true,
          },
      st.model_cached
        ? {
            kind: 'ok',
            title: 'Modello AI pronto',
            desc: 'Già presente su questo computer: nessun download necessario.',
          }
        : {
            kind: 'info',
            title: 'Download iniziale del modello AI',
            desc: 'Al primo avvio verrà scaricato una sola volta (~1,5 GB): serve una connessione internet.',
          },
    ];

    host.replaceChildren(...rows.map((r) => {
      const icName = r.kind === 'ok' ? 'check_circle' : r.kind === 'warning' ? 'warning' : 'info';
      const body = [h('p', { class: 'ob-check__title' }, r.title)];
      if (r.ffmpeg) {
        const cmd = 'winget install Gyan.FFmpeg';
        body.push(
          h('p', { class: 'ob-check__desc' },
            'Senza FFmpeg le foto funzionano comunque, ma non verranno create le anteprime dei video. Per installarlo da terminale:'),
          h('div', { class: 'ob-code-row' },
            h('code', { class: 'ob-code' }, cmd),
            h('md-text-button', {
              onclick: async () => {
                try {
                  await navigator.clipboard.writeText(cmd);
                  showSnackbar('Comando copiato negli appunti.');
                } catch {
                  showSnackbar('Copia il comando manualmente.');
                }
              },
            }, icon('content_copy', { slot: 'icon' }), 'Copia')));
      } else {
        body.push(h('p', { class: 'ob-check__desc' }, r.desc));
      }
      return h('div', { class: `ob-check ob-check--${r.kind}` },
        h('span', { class: 'ob-check__icon', 'aria-hidden': 'true' }, icon(icName)),
        h('div', { class: 'ob-check__body' }, body));
    }));
  }

  function reducedStep() {
    wiz.hint = h('p', { class: 'ob-hint', 'aria-live': 'polite' });
    return [
      intro(
        'Cambia la cartella delle foto',
        `Il tuo archivio ha già ${formatNumber(wiz.indexed)} file indicizzati. La nuova cartella verrà usata dalla prossima analisi.`,
      ),
      sourceFieldBlock(),
      wiz.hint,
    ];
  }

  // ---------------------------------------------------------- stepper & aside
  function buildStepper(current) {
    const items = STEPS.map((label, i) => {
      const done = i < current;
      const isCur = i === current;
      const btn = h('button', {
        type: 'button',
        class: 'ob-stepper__btn',
        disabled: !done,
        'aria-label': done ? `Torna al passo ${i + 1}: ${label}` : label,
        onclick: () => { if (done) goTo(i); },
      },
      h('span', { class: 'ob-stepper__dot', 'aria-hidden': 'true' }, done ? icon('check') : String(i + 1)),
      h('span', { class: 'ob-stepper__label' }, label));

      return h('li', {
        class: `ob-stepper__item${done ? ' is-done' : ''}${isCur ? ' is-current' : ''}`,
        'aria-current': isCur ? 'step' : null,
      }, btn);
    });

    const bar = document.createElement('md-linear-progress');
    bar.className = 'ob-stepper__bar';
    bar.value = (current + 1) / STEPS.length;
    bar.setAttribute('aria-hidden', 'true');

    return h('div', { class: 'ob-stepper' },
      h('ol', { class: 'ob-stepper__list', 'aria-label': `Passo ${current + 1} di ${STEPS.length}` }, items),
      bar);
  }

  let asideAnimated = false;
  function buildAside() {
    const idx = wiz.reduced ? 1 : wiz.step;
    const copy = ASIDE_COPY[idx] || ASIDE_COPY[0];
    const wm = createWordmark({
      className: 'ob-aside__wordmark',
      strokeWidth: 11,
      variant: 'hero',
      animated: !asideAnimated,
    });
    asideAnimated = true;
    return h('aside', { class: 'ob-aside', 'aria-hidden': 'true' },
      h('div', { class: 'ob-aside__brand' },
        h('img', { class: 'ob-aside__logo', src: 'favicon.svg', alt: '', width: 38, height: 38 }),
        wm),
      h('div', { class: 'ob-aside__visual' },
        asideArt(idx),
        h('div', {},
          h('p', { class: 'ob-aside__quote' }, copy.quote),
          h('p', { class: 'ob-aside__sub' }, copy.sub))),
      h('div', { class: 'ob-aside__trust' },
        icon('lock'),
        h('span', {}, '100% locale · Originali intatti')));
  }

  // ---------------------------------------------------------- navigazione
  async function stepReady() {
    if (wiz.step === 1 && wiz.src.state === 'empty' && wiz.source.trim()) await checkSource();
    if (wiz.step === 1) return wiz.src.state === 'ok';
    return true;
  }

  async function go(delta) {
    if (delta > 0 && !(await stepReady())) return;
    dir = delta;
    wiz.step = Math.max(0, Math.min(STEPS.length - 1, wiz.step + delta));
    render();
  }

  function goTo(target) {
    if (target === wiz.step || target < 0 || target >= STEPS.length) return;
    dir = target > wiz.step ? 1 : -1;
    wiz.step = target;
    render();
  }

  function buildHeader() {
    themeBtn = h('md-icon-button', {
      class: 'ob-head__theme',
      onclick: () => applyTheme(THEME_NEXT[store.get('theme', 'system')] || 'system'),
    });
    paintThemeBtn();

    return h('header', { class: 'ob-head' },
      h('span', { class: 'ob-head__brand-mobile' },
        h('img', { class: 'ob-head__logo', src: 'favicon.svg', alt: '', width: 28, height: 28 }),
        createWordmark({ className: 'ob-head__wordmark', strokeWidth: 11, variant: 'brand' })),
      h('span', { class: 'ob-head__pill' }, wiz.reduced ? 'Impostazioni rapide' : `Passo ${wiz.step + 1} di ${STEPS.length}`),
      h('span', { class: 'spacer' }),
      themeBtn,
      wiz.reduced ? null : h('md-text-button', { onclick: leave }, 'Salta per ora'));
  }

  function buildFooter() {
    if (wiz.reduced) {
      nextBtn = h('md-filled-button', { onclick: saveReduced }, icon('check', { slot: 'icon' }), 'Salva');
      return h('footer', { class: 'ob-foot' },
        h('md-text-button', { onclick: leave }, 'Annulla'),
        h('span', { class: 'spacer' }),
        nextBtn);
    }

    const last = wiz.step === STEPS.length - 1;
    const back = wiz.step > 0
      ? h('md-outlined-button', { onclick: () => go(-1) }, icon('arrow_back', { slot: 'icon' }), 'Indietro')
      : null;

    if (last) {
      nextBtn = h('md-filled-button', { onclick: start }, icon('play_arrow', { slot: 'icon' }), 'Inizia l’analisi');
    } else {
      nextBtn = h('md-filled-button', { onclick: () => go(1), 'trailing-icon': true },
        wiz.step === 0 ? 'Inizia' : 'Avanti',
        icon('arrow_forward', { slot: 'icon' }));
    }

    return h('footer', { class: 'ob-foot' },
      back,
      h('span', { class: 'spacer' }),
      nextBtn);
  }

  function render() {
    wiz.srcField = null;
    wiz.folderBadge = null;
    wiz.hint = null;

    const body = wiz.reduced
      ? reducedStep()
      : [welcomeStep, sourceStep, checksStep][wiz.step]();

    const panel = h('section', { class: `ob-panel ${dir >= 0 ? 'is-fwd' : 'is-back'}` }, body);
    const workspace = h('div', { class: 'ob-workspace' },
      buildHeader(),
      wiz.reduced ? null : buildStepper(wiz.step),
      h('div', { class: 'ob-body' }, panel),
      buildFooter());

    stage.replaceChildren(buildAside(), workspace);
    paintSource();
    updateNext();

    requestAnimationFrame(() => {
      const t = stage.querySelector('.ob-title');
      if (t) t.focus({ preventScroll: true });
    });
  }

  // ---------------------------------------------------------- salvataggio e avvio
  async function saveReduced() {
    if (!nextBtn) return;
    nextBtn.disabled = true;
    try {
      await api.put('/settings', { source_dir: wiz.source.trim() });
      try { store.set('status', await api.get('/status')); } catch { /* non bloccante */ }
      showSnackbar('Cartella delle foto aggiornata.');
      leave();
    } catch (e) {
      if (alive) {
        showSnackbar(e.message || 'Salvataggio non riuscito.', { error: true });
        nextBtn.disabled = false;
      }
    }
  }

  async function start() {
    if (!nextBtn) return;
    nextBtn.disabled = true;
    try {
      const payload = { source_dir: wiz.source.trim() };
      if (wiz.facesAvailable) payload.enable_faces = wiz.enableFaces;
      await api.put('/settings', payload);
      await api.post('/index/start', wiz.scope === 'quick' ? { limit: QUICK_LIMIT } : {});
    } catch (e) {
      if (e.status !== 409) {
        if (alive) {
          showSnackbar(e.message || 'Avvio non riuscito.', { error: true });
          nextBtn.disabled = false;
        }
        return;
      }
      showSnackbar('L’analisi è già in corso.');
    }
    try { store.set('status', await api.get('/status')); } catch { /* non bloccante */ }
    close();
    navigate('/index');
  }

  // ---------------------------------------------------------- avvio
  render();
  if (wiz.source.trim()) checkSource();
  return close;
}
