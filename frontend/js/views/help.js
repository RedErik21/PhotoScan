// Aiuto: centro assistenza. Come funziona, sicurezza dei file, domande frequenti con ricerca,
// scorciatoie reali dell'app, informazioni di sistema e copia diagnostica.
import { api } from '../api.js';
import { h, icon } from '../ui/el.js';
import { emptyState, errorState } from '../ui/empty-state.js';
import { skeletonCard } from '../ui/skeleton.js';
import { showSnackbar } from '../ui/snackbar.js';
import { openSupportDialog } from '../ui/support.js';

/** Carica css/view-help.css una sola volta. */
function loadCss() {
  if (document.querySelector('link[data-view-css="help"]')) return;
  const link = document.createElement('link');
  link.rel = 'stylesheet';
  link.href = new URL('../../css/view-help.css', import.meta.url).href;
  link.dataset.viewCss = 'help';
  document.head.append(link);
}

const STEPS = [
  { icon: 'neurology', title: 'Analisi', text: 'L’app legge foto e video, trova le date e riconosce i soggetti. Puoi fermarla quando vuoi.', href: '#/organize?tab=index' },
  { icon: 'cleaning_services', title: 'Libera spazio', text: 'Trova duplicati, video pesanti, screenshot e scontrini da spostare nel Cestino di Windows.', href: '#/organize?tab=cleanup' },
  { icon: 'collections_bookmark', title: 'Album', text: 'Raccogli le foto in album personali o esplora gli album suggeriti dai temi ricorrenti.', href: '#/albums' },
];

const FAQ = [
  {
    q: 'Perché non ogni giorno diventa un evento?',
    a: [
      'Un evento ha una cartella propria solo se ha almeno 15 foto e se nel suo giorno più pieno hai scattato almeno 4 volte le foto di un giorno tipico dell’anno.',
      'Tutte le altre foto del mese finiscono in un’unica cartella del mese, per esempio «2019/10 - Ottobre». Non è un errore: sono foto che non formano un evento abbastanza ricco.',
      'La soglia «quanto deve essere speciale un giorno» si regola nelle Impostazioni.',
    ],
    link: { href: '#/settings?tab=general', label: 'Apri Impostazioni' },
  },
  {
    q: 'Come vengono raggruppate le foto in eventi?',
    a: [
      'Le foto vengono ordinate per data. Un nuovo evento inizia quando passano più di 24 ore senza foto. Un evento dura al massimo 7 giorni: se è più lungo, viene diviso nella pausa più lunga.',
      'Il nome dell’evento contiene le date, poi la città (ricavata dal GPS, se diversa da quella di casa) e il tema prevalente, per esempio «2018/08 - 10-16 agosto - Chania - Mare e spiaggia».',
      'Se preferisci nomi con la sola data, come «Evento 2018-08-10», puoi cambiarlo nelle Impostazioni.',
    ],
    link: { href: '#/settings?tab=general', label: 'Apri Impostazioni' },
  },
  {
    q: 'Dove finiscono i duplicati?',
    a: [
      'Le copie esatte (stesso contenuto, bit per bit) vanno nella cartella _Duplicati. Le foto quasi uguali (ridimensionate, compresse o ritagliate) vanno in _Duplicati/Simili.',
      'Nessuna foto viene cancellata: gli originali restano dove sono, e puoi decidere quale tenere dalla pagina Duplicati. In Impostazioni, sezione Generale, puoi scegliere di non creare queste cartelle.',
    ],
    link: { href: '#/duplicates', label: 'Apri Duplicati' },
  },
  {
    q: 'Cos’è «Data incerta»?',
    a: [
      'Significa che l’app non ha trovato una data affidabile: né nei dati della foto (EXIF), né nel file JSON di Google Foto, né nel nome del file.',
      'In questo caso usa la data di modifica del file, che può non coincidere con il giorno dello scatto. Le foto con data incerta vanno nella cartella «Data incerta» delle Cartelle proposte.',
    ],
    link: { href: '#/settings?tab=general', label: 'Apri Impostazioni' },
  },
  {
    q: 'Come viene scelta la data di una foto?',
    a: [
      'L’app prova queste fonti, in quest’ordine:',
      '1. Dati EXIF: la data di scatto registrata dalla fotocamera o dal telefono (per le foto).',
      '2. File JSON di Google Foto: la data salvata da Google accanto alla foto. Ha la precedenza sulla data dei metadati di un video.',
      '3. Metadati del video: la data di creazione registrata nel video (solo se non c’è un JSON di Google Foto).',
      '4. Nome del file: se contiene una data, come IMG_20200609_092806.',
      '5. Data di modifica del file: l’ultima risorsa, la meno affidabile.',
    ],
  },
  {
    q: 'Come posso migliorare i contesti?',
    a: [
      'I contesti («Mare», «Montagna», «Neve»…) sono etichette che l’app assegna confrontando ogni foto con una descrizione in linguaggio naturale.',
      'In Impostazioni, sezione Etichette, puoi cambiare la descrizione di un contesto (per esempio «spiaggia con mare e sabbia») e provarla prima di salvare: l’app mostra le foto che corrispondono. Dopo le modifiche, ricalcola le cartelle proposte.',
    ],
    link: { href: '#/settings?tab=labels', label: 'Apri Etichette' },
  },
  {
    q: 'Posso interrompere l’indicizzazione?',
    a: [
      'Sì. Nella pagina Analisi premi «Ferma». L’operazione è riprendibile: alla partenza successiva riprende da dove si era fermata, senza ricominciare da capo.',
      'Fermarla non tocca le tue foto.',
    ],
    link: { href: '#/index', label: 'Apri Analisi' },
  },
  {
    q: 'Come funziona il riconoscimento volti (Persone)?',
    a: [
      'Il riconoscimento volti raggruppa automaticamente le persone presenti nelle tue foto. Tutta l’elaborazione avviene in locale sul tuo PC (con scheda video NVIDIA o processore): nessuna foto viene mai inviata online.',
      'Per usarlo, attiva «Riconoscimento volti» in Impostazioni, scarica i modelli locali (~280 MB, una sola volta) e avvia l’Analisi delle foto. Nella sezione Persone puoi dare un nome a ciascuno, unire più gruppi della stessa persona, nascondere volti o correggere eventuali errori.',
    ],
    link: { href: '#/people', label: 'Apri Persone' },
  },
  {
    q: 'Dove sono i miei dati?',
    a: [
      'Le tue foto restano nella cartella originale: l’app la legge soltanto e non la modifica mai.',
      'Il catalogo (il file catalogo.db con l’elenco e le informazioni delle foto), la cache delle miniature e il file di configurazione (config.json) si trovano nella cartella dell’app. Le cartelle create con «Crea cartelle» si trovano nella destinazione che hai scelto.',
    ],
  },
  {
    q: 'Come si disinstalla?',
    a: [
      'Chiudi l’app e rimuovi la sua cartella. Le foto originali e le cartelle già create restano dove sono: la disinstallazione non le tocca.',
      'Se vuoi eliminare anche il catalogo e la cache, cancellali insieme alla cartella dell’app, dopo averla chiusa.',
    ],
  },
];

const SHORTCUTS = [
  { group: 'Ovunque', rows: [
    { keys: ['/'], text: 'Porta il cursore nel campo di ricerca' },
    { keys: ['?'], text: 'Apre questa pagina di aiuto' },
    { keys: ['g', 'poi', 'h'], text: 'Vai alla Home (entro un secondo)' },
    { keys: ['g', 'poi', 'f'], text: 'Vai alle Foto' },
    { keys: ['g', 'poi', 's'], text: 'Vai a Cerca' },
    { keys: ['Invio'], text: 'Nel campo di ricerca: avvia la ricerca' },
  ] },
  { group: 'Griglie di foto', rows: [
    { keys: ['←', '↑', '→', '↓'], text: 'Sposta il riquadro selezionato' },
    { keys: ['PgSu', 'PgGiù'], text: 'Scorre di una schermata' },
    { keys: ['Home', 'End'], text: 'Va alla prima o all’ultima foto' },
    { keys: ['Invio'], text: 'Apre la foto selezionata' },
    { keys: ['x'], text: 'Seleziona o deseleziona la foto' },
    { keys: ['f'], text: 'Aggiunge o toglie dai preferiti' },
    { keys: ['Esc'], text: 'Annulla la selezione' },
    { keys: ['↑', '↓'], text: 'Barra delle date: scorre la timeline' },
  ] },
  { group: 'Visualizzatore', rows: [
    { keys: ['←', '→'], text: 'Foto precedente e successiva' },
    { keys: ['Esc'], text: 'Chiude il visualizzatore' },
    { keys: ['Home', 'End'], text: 'Prima e ultima foto' },
    { keys: ['+', '-'], text: 'Ingrandisce e riduce' },
    { keys: ['0'], text: 'Torna allo zoom normale' },
    { keys: ['i'], text: 'Mostra o nasconde le informazioni' },
    { keys: ['f'], text: 'Aggiunge o toglie dai preferiti' },
    { keys: ['a'], text: 'Aggiunge la foto a un album' },
  ] },
  { group: 'Duplicati', rows: [
    { keys: ['←', '→'], text: 'Nel confronto: gruppo precedente e successivo' },
    { keys: ['↑', '↓'], text: 'Nell’elenco: sposta il gruppo selezionato' },
    { keys: ['k'], text: 'Tiene la versione più grande del gruppo' },
    { keys: ['Esc'], text: 'Chiude il confronto' },
  ] },
];

const VOWELS = { a: 'aàá', e: 'eèé', i: 'iìí', o: 'oòó', u: 'uùú' };

/** Espressione per cercare un termine ignorando maiuscole e accenti (per filtro ed evidenziazione). */
function termPattern(term) {
  return [...term].map((c) => {
    const lc = c.toLowerCase();
    if (VOWELS[lc]) return `[${VOWELS[lc]}${VOWELS[lc].toUpperCase()}]`;
    return c.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  }).join('');
}

/** Ritorna i nodi di testo con i termini evidenziati in <mark>. */
function highlight(text, tokens) {
  if (!tokens.length) return [text];
  const re = new RegExp(tokens.map(termPattern).join('|'), 'gi');
  const out = [];
  let last = 0;
  for (const m of text.matchAll(re)) {
    if (m.index > last) out.push(text.slice(last, m.index));
    out.push(h('mark', {}, m[0]));
    last = m.index + m[0].length;
  }
  if (last < text.length) out.push(text.slice(last));
  return out;
}

/** True se ogni termine compare nel testo (filtro FAQ). */
function matches(text, tokens) {
  return tokens.every((t) => new RegExp(termPattern(t), 'i').test(text));
}

function section(id, title, ...children) {
  return h('section', { class: 'section help-section', 'aria-labelledby': id },
    h('h2', { id, class: 'section-title md-typescale-title-large' }, title),
    children);
}

function renderSteps() {
  return h('ol', { class: 'help-steps' }, STEPS.map((s, i) => h('li', { class: 'help-step card' },
    h('span', { class: 'help-step__icon', 'aria-hidden': 'true' }, icon(s.icon)),
    h('span', { class: 'help-step__num md-typescale-label-medium' }, `Passo ${i + 1}`),
    h('h3', { class: 'help-step__title md-typescale-title-medium' }, s.title),
    h('p', { class: 'help-step__text md-typescale-body-medium' }, s.text),
    h('a', { class: 'help-step__link md-typescale-label-large', href: s.href, 'aria-label': `Apri ${s.title}` }, 'Apri', icon('arrow_forward')))));
}

function renderSafety() {
  return h('div', { class: 'help-safety' },
    h('div', { class: 'callout callout--info' },
      h('span', { class: 'callout__icon' }, icon('shield')),
      h('div', {},
        h('p', { class: 'callout__title md-typescale-title-small' }, 'Le tue foto non vengono mai toccate'),
        h('p', { class: 'md-typescale-body-medium' },
          'L’app non cancella, non sposta e non modifica gli originali: li legge soltanto. Le cartelle nuove vengono create solo quando confermi, nella destinazione che scegli tu.'))),
    h('article', { class: 'card help-card' },
      h('h3', { class: 'md-typescale-title-medium' }, 'Cos’è un hard link?'),
      h('p', { class: 'md-typescale-body-medium' },
        'È un secondo nome dato allo stesso file sul disco. Non è una copia: il contenuto occupa lo spazio una sola volta.'),
      h('p', { class: 'md-typescale-body-medium' },
        'Pensa a un libro con due indici che portano alla stessa pagina: la pagina è una sola, anche se la trovi da due parti.'),
      h('pre', { class: 'code-block help-example' },
        'Foto originali\\IMG_0624.HEIC\n2018\\08 - 10-16 agosto - Chania\\IMG_0624.HEIC'),
      h('p', { class: 'md-typescale-body-medium' },
        'Nell’esempio la stessa foto compare in due cartelle, ma sul disco esiste una volta sola.'),
      h('h4', { class: 'md-typescale-title-small' }, 'Quando usare la copia'),
      h('p', { class: 'md-typescale-body-medium' },
        'Gli hard link funzionano solo all’interno dello stesso disco. Se la destinazione è su un altro disco o in rete, l’app copia il file: i due file sono indipendenti, occupano più spazio, e modificarne uno non tocca l’altro. PhotoScan sceglie da sola in base ai dischi; puoi cambiarlo in Impostazioni, sezione Generale, o nella pagina Crea cartelle.'),
      h('p', { class: 'md-typescale-body-medium' },
        'Attenzione: con un hard link, una modifica fatta in un collegamento vale anche per l’originale.')),
    h('article', { class: 'card help-card' },
      h('h3', { class: 'md-typescale-title-medium' }, 'Identico o quasi uguale?'),
      h('div', { class: 'compare' },
        h('div', { class: 'card help-compare-item' },
          h('h4', { class: 'md-typescale-title-small' }, 'Identico'),
          h('p', { class: 'md-typescale-body-medium' }, 'Stesso contenuto, bit per bit. Una delle due copie è superflua e può essere tolta senza perdite.')),
        h('div', { class: 'card help-compare-item' },
          h('h4', { class: 'md-typescale-title-small' }, 'Quasi uguale'),
          h('p', { class: 'md-typescale-body-medium' }, 'Stessa immagine con qualche differenza: ridimensionata, compressa, ritagliata o ruotata. L’app le mostra affiancate e decidi tu quale tenere.')))));
}

function renderFaqItem(item, tokens) {
  const hay = [item.q, ...item.a].join(' ');
  const body = h('div', { class: 'help-faq__body' },
    item.a.map((p) => h('p', { class: 'md-typescale-body-medium' }, highlight(p, tokens))),
    item.link ? h('a', { class: 'help-card__link md-typescale-label-large', href: item.link.href }, item.link.label, icon('arrow_forward')) : null);
  return {
    hay,
    el: h('details', { class: 'help-faq__item' },
      h('summary', { class: 'md-typescale-title-small' },
        h('span', {}, highlight(item.q, tokens)),
        h('span', { class: 'help-faq__chev', 'aria-hidden': 'true' }, icon('expand_more'))),
      body),
  };
}

function renderFaqSection() {
  const field = h('md-outlined-text-field', { class: 'help-search', label: 'Cerca nelle domande', type: 'search' });
  field.append(icon('search', { slot: 'leading-icon' }));
  const list = h('div', { class: 'help-faq', id: 'help-faq-list' });

  function render() {
    const tokens = (field.value || '').trim().toLowerCase().split(/\s+/).filter(Boolean);
    const visible = FAQ.map((f) => renderFaqItem(f, tokens))
      .filter((r) => !tokens.length || matches(r.hay, tokens));
    if (!visible.length) {
      list.replaceChildren(emptyState({
        icon: 'search_off', title: 'Nessuna domanda trovata',
        message: 'Prova con una parola diversa, oppure controlla le scorciatoie qui sotto.',
      }));
      return;
    }
    list.replaceChildren(...visible.map((r) => {
      if (tokens.length) r.el.open = true;
      return r.el;
    }));
  }
  field.addEventListener('input', render);
  render();
  return section('help-faq-title', 'Domande frequenti', h('div', { class: 'help-faq-tools' }, field), list);
}

/** Tasti come .kbd; la voce 'poi' indica una sequenza (g poi h). */
function kbd(keys) {
  return h('span', { class: 'help-keys' }, keys.map((k) => (k === 'poi'
    ? h('span', { class: 'help-keys__sep' }, 'poi')
    : h('span', { class: 'kbd' }, k))));
}

function renderShortcuts() {
  const tbody = SHORTCUTS.map((g) => h('tbody', {},
    h('tr', {}, h('th', { colspan: 2, scope: 'colgroup', class: 'help-group' }, g.group)),
    g.rows.map((r) => h('tr', {},
      h('td', {}, kbd(r.keys)),
      h('td', { class: 'md-typescale-body-medium' }, r.text)))));
  return section('help-keys-title', 'Scorciatoie da tastiera',
    h('p', { class: 'md-typescale-body-medium help-muted' }, 'Le scorciatoie funzionano con una tastiera fisica, non quando stai scrivendo in un campo di testo.'),
    h('div', { class: 'help-table-wrap' },
      h('table', { class: 'table-m3 help-table' },
        h('caption', { class: 'sr-only' }, 'Scorciatoie da tastiera per schermata'),
        h('thead', {}, h('tr', {}, h('th', { scope: 'col' }, 'Tasto'), h('th', { scope: 'col' }, 'Cosa fa'))),
        tbody)));
}

/** Copia negli appunti; ripiega su un campo temporaneo se l'API non e' disponibile. */
async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    const ta = h('textarea', { readOnly: true, style: { position: 'fixed', opacity: '0' } });
    ta.value = text;
    document.body.append(ta);
    ta.select();
    let ok = false;
    try { ok = document.execCommand('copy'); } catch { ok = false; }
    ta.remove();
    return ok;
  }
}

function infoRow(label, value) {
  return h('div', { class: 'help-info__row' },
    h('dt', { class: 'md-typescale-label-large' }, label),
    h('dd', { class: 'md-typescale-body-medium' }, value));
}

function openFolderButton(path) {
  return h('md-text-button', {
    onclick: async () => {
      try { await api.post('/system/open-folder', { path }); }
      catch (e) { showSnackbar(e.message || 'Impossibile aprire la cartella.', { error: true }); }
    },
  }, icon('folder_open', { slot: 'icon' }), 'Apri cartella');
}

function copyPathButton(path) {
  return h('md-text-button', {
    onclick: async () => {
      const ok = await copyText(path);
      showSnackbar(ok ? 'Percorso copiato.' : 'Copia non riuscita.', { error: !ok });
    },
  }, icon('content_copy', { slot: 'icon' }), 'Copia percorso');
}

function renderInfo(st) {
  const pathRow = (label, path) => path
    ? infoRow(label, h('span', { class: 'help-info__value' },
      h('span', { class: 'help-path' }, path),
      h('span', { class: 'help-info__actions' }, copyPathButton(path))))
    : null;
  const folderRow = (label, path, suffix = '') => path
    ? infoRow(label, h('span', { class: 'help-info__value' },
      h('span', { class: 'help-path' }, `${path}${suffix}`),
      h('span', { class: 'help-info__actions' }, copyPathButton(path), openFolderButton(path))))
    : infoRow(label, 'Non impostata');
  return h('dl', { class: 'help-info' },
    infoRow('Versione', st.version || '—'),
    infoRow('Modello di riconoscimento', st.model || '—'),
    infoRow('Calcolo', `${st.device || '—'}${st.model_cached === false ? ' · modello non ancora scaricato' : ''}`),
    infoRow('Video (ffmpeg)', st.ffmpeg ? 'Disponibile' : 'Non trovato: alcuni video potrebbero non avere anteprima'),
    folderRow('Cartella originali (sola lettura)', st.source_dir, st.source_network ? ' (rete)' : ''),
    folderRow('Cartella destinazione', st.output_dir),
    pathRow('Catalogo', st.db_path),
    infoRow('Registro indicizzazione', 'cache/index.log nella cartella dell’app'),
    infoRow('Licenze open source', h('span', { class: 'help-info__value' },
      'SigLIP 2 (Apache License 2.0) · Material Web (Apache License 2.0) · PyTorch (BSD-3-Clause)')));
}

/** Testo leggibile per l'assistenza: versione, conteggi, ultimi errori. */
function diagnosticText(st, errors) {
  const c = st.counts || {};
  const lines = [
    'PhotoScan diagnostica',
    `Data: ${new Date().toISOString()}`,
    `Versione: ${st.version}`,
    `Dispositivo: ${st.device}`,
    `ffmpeg: ${st.ffmpeg ? 'sì' : 'no'}`,
    `Modello: ${st.model} (in cache: ${st.model_cached ? 'sì' : 'no'})`,
    `Conteggi: totale ${c.totale}, foto ${c.foto}, video ${c.video}, indicizzati ${c.indicizzati}, errori ${c.errori}, duplicati ${c.duplicati}, data incerta ${c.data_incerta}, senza data EXIF ${c.senza_data_exif}`,
    `Piano: ${st.plan.built ? `costruito (${st.plan.folders} cartelle, ${st.plan.files} file)` : 'non costruito'}`,
    `Ultimi errori (fino a 5): ${errors.length ? '' : 'nessuno'}`,
    ...errors.map((e) => `- ${e.path}: ${e.error}`),
  ];
  return lines.join('\n');
}

function renderDiagnostics(getStatus) {
  const note = h('p', { class: 'md-typescale-body-medium help-muted' },
    'Copia un riepilogo tecnico da inviare in caso di problemi. Contiene anche i percorsi dei file con errore: controllalo prima di condividerlo.');
  const btn = h('md-filled-tonal-button', {
    onclick: async () => {
      const st = getStatus();
      if (!st) {
        showSnackbar('Le informazioni di sistema non sono ancora disponibili.', { error: true });
        return;
      }
      btn.disabled = true;
      try {
        const errors = await api.get('/index/errors', { limit: 5000 }).catch(() => []);
        const ok = await copyText(diagnosticText(st, errors.slice(-5)));
        showSnackbar(ok ? 'Diagnostica copiata negli appunti.' : 'Copia non riuscita.', { error: !ok });
      } catch (e) {
        showSnackbar(e.message || 'Impossibile preparare la diagnostica.', { error: true });
      } finally {
        btn.disabled = false;
      }
    },
  }, icon('content_copy', { slot: 'icon' }), 'Copia diagnostica');
  return h('div', { class: 'help-diagnostics' }, note, btn);
}

export async function mount(root, { signal } = {}) {
  loadCss();
  const infoBody = h('div', { class: 'help-info-body', 'aria-live': 'polite' }, skeletonCard());
  let status = null;

  const page = h('div', { class: 'help-page' },
    h('header', { class: 'help-header' },
      h('h1', { class: 'md-typescale-headline-medium' }, 'Aiuto'),
      h('p', { class: 'md-typescale-body-large help-muted' },
        'Come funziona PhotoScan, perché i tuoi file sono al sicuro e le risposte alle domande più comuni.')),
    section('help-how-title', 'Come funziona', renderSteps()),
    section('help-safe-title', 'La tua sicurezza', renderSafety()),
    renderFaqSection(),
    renderShortcuts(),
    section('help-info-title', 'Informazioni', infoBody,
      h('article', { class: 'card help-support-card' },
        h('div', { class: 'help-support-card__copy' },
          h('h3', { class: 'md-typescale-title-medium' }, 'Sostieni lo sviluppo'),
          h('p', { class: 'md-typescale-body-medium help-muted' },
            'PhotoScan è gratuito, open source e 100% locale. Se ti è utile, puoi aiutare lo sviluppatore con un supporto volontario.')),
        h('md-filled-tonal-button', { onclick: () => openSupportDialog() },
          icon('favorite', { slot: 'icon', filled: true }), 'Aiuta lo Sviluppatore')),
      h('div', { class: 'help-diag-slot' }, renderDiagnostics(() => status))));

  root.replaceChildren(h('div', { class: 'view help-view' }, page));

  try {
    status = await api.get('/status', undefined, signal);
    if (signal?.aborted) return;
    infoBody.replaceChildren(renderInfo(status));
  } catch (e) {
    if (e.name === 'AbortError' || signal?.aborted) return;
    infoBody.replaceChildren(errorState({
      title: 'Informazioni non disponibili',
      message: e.message || 'Il server non ha risposto.',
      onRetry: () => mount(root, { signal }),
    }));
  }
}
