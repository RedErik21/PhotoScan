// NAS: stato e spazio del disco di rete, invio verificato con hash, ricezione sul PC,
// pulizia sicura nel Cestino e registro delle operazioni.
import { h, icon } from '../ui/el.js';
import { api, ApiError, onEvent } from '../api.js';
import { showSnackbar } from '../ui/snackbar.js';
import { errorState } from '../ui/empty-state.js';
import { skeletonCard } from '../ui/skeleton.js';
import { formatBytes, formatDateTime, formatNumber, plural } from '../ui/format.js';

const LAYOUTS = [
  { id: 'as_is', label: 'Come gli originali', hint: 'Mantiene la stessa struttura di cartelle del PC.' },
  { id: 'by_album', label: 'Cartelle per album', hint: 'Una cartella per album; il resto finisce in «_senza_album».' },
];

const JOB_TITLES = {
  nas_send: 'Invio e verifica sul NAS',
  nas_receive: 'Ricezione dal NAS',
  nas_cleanup: 'Spostamento nel Cestino',
};

const LOG_LABELS = {
  copia_verificata: { text: 'Copiato', cls: 'ok' },
  presente_verificato: { text: 'Verificato', cls: 'ok' },
  ricevuto: { text: 'Ricevuto', cls: 'ok' },
  cestino: { text: 'Nel Cestino', cls: 'neutral' },
  conflitto: { text: 'Conflitto', cls: 'down' },
  conflitto_ricezione: { text: 'Conflitto', cls: 'down' },
  saltato_copia_non_valida: { text: 'Saltato', cls: 'down' },
  saltato_locale_cambiato: { text: 'Modificato', cls: 'down' },
  errore: { text: 'Errore', cls: 'down' },
  errore_ricezione: { text: 'Errore', cls: 'down' },
  errore_cestino: { text: 'Errore', cls: 'down' },
};

function loadCss() {
  if (document.querySelector('link[data-view-css="nas"]')) return;
  const link = document.createElement('link');
  link.rel = 'stylesheet';
  link.href = new URL('../../css/view-nas.css', import.meta.url).href;
  link.dataset.viewCss = 'nas';
  document.head.append(link);
}

function errText(e) {
  if (e instanceof ApiError) return e.message;
  return e?.message || 'Operazione non riuscita.';
}

// Stato condiviso tra cambi vista: i job continuano sul server e vengono riagganciati.
const S = {
  status: null,
  plan: null,
  preview: null,
  log: [],
  layout: null,
  jobId: null,
  job: null,
  busy: false,
  refreshing: false,
  nasDraft: null,
  recvDraft: null,
  filterAlbums: false,
  selectedAlbumIds: new Set(),
  albumsInited: false,
  includeUnsorted: true,
};
let UI = null;

function pill(text, cls, withDot = false) {
  return h('span', { class: `nas-pill nas-pill--${cls}` },
    withDot ? h('span', { class: 'nas-pill__dot', 'aria-hidden': 'true' }) : null,
    text);
}

function spaceBar(used, total) {
  const pct = total ? Math.min(100, Math.round((used / total) * 100)) : 0;
  const tone = pct >= 90 ? ' nas-bar__fill--danger' : pct >= 75 ? ' nas-bar__fill--warn' : '';
  return h('div', { class: 'nas-bar', role: 'img', 'aria-label': `Spazio occupato ${pct}%` },
    h('div', { class: `nas-bar__fill${tone}`, style: `width:${pct}%` }));
}

async function pickFolderInto(title, initial, onPicked) {
  try {
    const res = await api.post('/system/pick-folder', { title, initial: initial || undefined });
    if (res && res.available === false) {
      showSnackbar('Finestra di scelta cartella non disponibile: incolla il percorso manualmente.', { error: true });
      return;
    }
    if (res?.path) onPicked(res.path);
  } catch (e) {
    showSnackbar(errText(e), { error: true });
  }
}

async function loadStatus() {
  const st = await api.get('/nas/status', null, UI?.ctx?.signal);
  S.status = st;
  S.layout = S.layout || st.layout || 'as_is';
  if (S.nasDraft === null) S.nasDraft = st.nas_dir || '';
  if (S.recvDraft === null) S.recvDraft = st.receive_dir || '';
  // Riaggancia un eventuale job NAS gia' in corso sul server
  if (st.active_job && (!S.job || S.job.state !== 'running')) {
    S.jobId = st.active_job.id;
    S.job = st.active_job;
  }
}

async function loadPlan() {
  if (!S.status?.nas_dir || !S.status?.space?.reachable) { S.plan = null; return; }
  const params = { layout: S.layout };
  if (S.layout === 'by_album' && S.filterAlbums) {
    params.album_ids = [...S.selectedAlbumIds].join(',');
    params.include_unsorted = S.includeUnsorted ? 1 : 0;
  }
  try {
    S.plan = await api.get('/nas/plan', params, UI?.ctx?.signal);
  } catch (e) {
    if (e?.name === 'AbortError') throw e;
    S.plan = { error: errText(e) };
  }
}

async function loadPreview() {
  try {
    S.preview = await api.get('/nas/cleanup', null, UI?.ctx?.signal);
  } catch (e) {
    if (e?.name === 'AbortError') throw e;
    S.preview = { count: 0, bytes: 0, sample: [], error: errText(e) };
  }
}

async function loadLog() {
  try {
    const rows = await api.get('/nas/log', { limit: 60 }, UI?.ctx?.signal);
    S.log = Array.isArray(rows) ? rows : [];
  } catch (e) {
    if (e?.name === 'AbortError') throw e;
    S.log = [];
  }
}

async function refreshAll() {
  S.refreshing = true;
  await loadStatus();
  await Promise.all([loadPlan(), loadPreview(), loadLog()]);
  S.refreshing = false;
  render();
}

function isJobRunning(name) {
  return S.job?.state === 'running' && (!name || S.job.name === name);
}

function startJob(path, body) {
  return async () => {
    if (S.busy) return;
    S.busy = true;
    render();
    try {
      const j = await api.post(path, body);
      S.jobId = j.id;
      S.job = j;
      render();
    } catch (e) {
      showSnackbar(errText(e), { error: true });
    } finally {
      S.busy = false;
      render();
    }
  };
}

// ---------- Sezione 1: Stato, percorso NAS, spazio e KPI ----------
function sectionOverview() {
  const cur = S.status || {};
  const sp = cur.space || {};
  const configured = Boolean(cur.nas_dir);
  const reachable = Boolean(configured && sp.reachable);
  const stateCls = !configured ? 'neutral' : reachable ? 'ok' : 'down';
  const stateTxt = !configured ? 'Non configurato' : reachable ? 'Online' : 'Non raggiungibile';

  const tf = document.createElement('md-outlined-text-field');
  tf.label = 'Cartella del NAS';
  tf.placeholder = 'Es. \\\\NAS\\foto  oppure  Z:\\foto';
  tf.value = S.nasDraft ?? (cur.nas_dir || '');
  tf.supportingText = 'Percorso di rete montato (UNC o unità mappata). Nulla sul NAS viene mai sovrascritto.';

  const saveBtn = h('md-filled-tonal-button', {
    disabled: tf.value.trim() === (cur.nas_dir || ''),
    onclick: async () => {
      saveBtn.disabled = true;
      try {
        const val = tf.value.trim();
        await api.put('/nas/settings', { nas_dir: val });
        S.nasDraft = val;
        showSnackbar('Cartella NAS salvata.');
        await refreshAll();
      } catch (e) {
        showSnackbar(errText(e), { error: true });
        saveBtn.disabled = false;
      }
    },
  }, icon('save', { slot: 'icon' }), 'Salva');

  tf.addEventListener('input', () => {
    S.nasDraft = tf.value;
    saveBtn.disabled = tf.value.trim() === (cur.nas_dir || '');
  });

  const browseBtn = h('md-outlined-button', {
    onclick: () => pickFolderInto('Scegli la cartella sul NAS', tf.value.trim(), (picked) => {
      tf.value = picked;
      S.nasDraft = picked;
      saveBtn.disabled = picked.trim() === (cur.nas_dir || '');
    }),
  }, icon('folder_open', { slot: 'icon' }), 'Sfoglia…');

  const used = sp.total_bytes != null && sp.free_bytes != null ? Math.max(0, sp.total_bytes - sp.free_bytes) : 0;
  const pctUsed = sp.total_bytes ? Math.min(100, Math.round((used / sp.total_bytes) * 100)) : 0;
  const totalCat = cur.catalog_files || 0;
  const verified = cur.verified_copies || 0;
  const covPct = totalCat > 0 ? Math.min(100, Math.round((verified / totalCat) * 100)) : 0;

  return h('section', { class: 'nas-card', 'aria-labelledby': 'nas-overview-title' },
    h('div', { class: 'nas-card__head' },
      h('div', { class: 'nas-card__title-wrap' },
        h('span', { class: 'nas-card__icon', 'aria-hidden': 'true' }, icon('dns')),
        h('h2', { id: 'nas-overview-title', class: 'md-typescale-title-medium' }, 'Disco di rete (NAS)')),
      pill(stateTxt, stateCls, true)),
    h('div', { class: 'nas-path-row' }, tf, browseBtn, saveBtn),
    configured && !reachable
      ? h('div', { class: 'callout callout--warning', role: 'alert' },
        icon('cloud_off'),
        h('div', {},
          h('p', { class: 'callout__title' }, 'Il NAS non risponde'),
          h('p', {}, 'Verifica che il NAS sia acceso, connesso alla rete locale e che la cartella condivisa sia raggiungibile.')))
      : null,
    reachable && sp.total_bytes != null
      ? h('div', { class: 'nas-space-box' },
        h('div', { class: 'nas-space-head' },
          h('span', { class: 'md-typescale-title-small' }, `${formatBytes(sp.free_bytes)} liberi`),
          h('span', { class: 'muted md-typescale-body-small' },
            `${formatBytes(used)} usati su ${formatBytes(sp.total_bytes)} (${pctUsed}%)`)),
        spaceBar(used, sp.total_bytes))
      : null,
    h('div', { class: 'nas-kpis' },
      h('div', { class: 'nas-kpi' },
        h('span', { class: 'nas-kpi__value' }, formatNumber(totalCat)),
        h('span', { class: 'nas-kpi__label md-typescale-body-small' }, 'File nel catalogo')),
      h('div', { class: 'nas-kpi' },
        h('span', { class: 'nas-kpi__value' }, formatNumber(verified)),
        h('span', { class: 'nas-kpi__label md-typescale-body-small' }, 'Copie verificate sul NAS'),
        totalCat > 0 ? h('span', { class: 'nas-kpi__sub md-typescale-label-small' }, `${covPct}% del catalogo`) : null),
      h('div', { class: 'nas-kpi' },
        h('span', { class: 'nas-kpi__value' }, formatNumber(cur.cleanup_ready || 0)),
        h('span', { class: 'nas-kpi__label md-typescale-body-small' }, 'Pronti da liberare sul PC'),
        cur.cleanup_bytes ? h('span', { class: 'nas-kpi__sub md-typescale-label-small' }, formatBytes(cur.cleanup_bytes)) : null)));
}

// ---------- Sezione 2: Banner job attivo o esito ----------
function sectionJob() {
  const j = S.job;
  if (!j) return null;
  const running = j.state === 'running';
  const isErr = j.state === 'error';
  const r = j.result || {};
  const title = JOB_TITLES[j.name] || j.stage || 'Operazione NAS';
  const pct = j.total > 0 ? Math.min(100, Math.round((j.done / j.total) * 100)) : null;

  const chips = [];
  if (!running && j.result) {
    if (j.name === 'nas_send') {
      chips.push(`Copiati e verificati: ${formatNumber(r.copiati || 0)}`);
      chips.push(`Già presenti verificati: ${formatNumber(r.verificati || 0)}`);
      if (r.conflitti) chips.push(`Conflitti (non toccati): ${formatNumber(r.conflitti)}`);
      if (r.errori) chips.push(`Errori: ${formatNumber(r.errori)}`);
    } else if (j.name === 'nas_receive') {
      chips.push(`Ricevuti: ${formatNumber(r.ricevuti || 0)}`);
      chips.push(`Già presenti: ${formatNumber(r.gia_presenti || 0)}`);
      if (r.conflitti) chips.push(`Conflitti: ${formatNumber(r.conflitti)}`);
      if (r.errori) chips.push(`Errori: ${formatNumber(r.errori)}`);
    } else if (j.name === 'nas_cleanup') {
      chips.push(`Spostati nel Cestino: ${formatNumber(r.spostati || 0)}`);
      if (r.saltati) chips.push(`Saltati: ${formatNumber(r.saltati)}`);
      if (r.errori) chips.push(`Errori: ${formatNumber(r.errori)}`);
    }
    if (r.annullato) chips.push('Operazione interrotta dall’utente');
  }

  const prog = document.createElement('md-linear-progress');
  prog.indeterminate = pct === null;
  if (pct !== null) prog.value = pct / 100;

  return h('section', {
    class: `nas-card nas-job-card${isErr ? ' nas-job-card--error' : ''}`,
    role: 'status',
    'aria-live': 'polite',
  },
    h('div', { class: 'nas-card__head' },
      h('span', { class: 'md-typescale-title-small' },
        running ? `${title} — in corso…` : j.state === 'done' ? `${title} — completato` : `${title} — ${j.state}`),
      running
        ? h('md-text-button', {
          onclick: async () => {
            try { await api.post(`/jobs/${j.id}/cancel`, {}); showSnackbar('Arresto richiesto…'); }
            catch (e) { showSnackbar(errText(e), { error: true }); }
          },
        }, 'Ferma')
        : h('md-text-button', { onclick: () => { S.job = null; S.jobId = null; render(); } }, 'Chiudi')),
    running ? prog : null,
    running ? h('p', { class: 'nas-desc md-typescale-body-small' },
      j.total ? `${formatNumber(j.done)} di ${formatNumber(j.total)} file (${pct}%)` : (j.message || 'Preparazione…')) : null,
    chips.length ? h('div', { class: 'nas-job-stats' }, ...chips.map((c) => h('span', { class: 'nas-job-chip' }, c))) : null,
    isErr ? h('p', { class: 'md-typescale-body-medium', style: 'color:var(--md-sys-color-error);margin:0;' },
      j.error || j.message || 'Operazione non riuscita.') : null);
}

// ---------- Sezione 3: Invia al NAS ----------
function sectionSend() {
  const p = S.plan;
  const counts = p?.counts || {};
  const noNas = !S.status?.nas_dir;
  const unreachable = !noNas && !S.status?.space?.reachable;
  const running = isJobRunning();

  const layoutGroup = h('div', { class: 'nas-layouts', role: 'radiogroup', 'aria-label': 'Struttura cartelle sul NAS' },
    ...LAYOUTS.map((o) => {
      const selected = S.layout === o.id;
      const radio = document.createElement('md-radio');
      radio.name = 'nas-layout';
      radio.value = o.id;
      radio.checked = selected;
      radio.setAttribute('aria-label', o.label);
      const pick = async () => {
        if (S.layout === o.id) return;
        S.layout = o.id;
        api.put('/nas/settings', { nas_layout: o.id }).catch(() => {});
        await loadPlan();
        render();
      };
      radio.addEventListener('change', () => { if (radio.checked) pick(); });
      return h('label', { class: `nas-layout-opt${selected ? ' is-selected' : ''}` },
        radio,
        h('span', { class: 'nas-layout-opt__text' },
          h('span', { class: 'md-typescale-label-large' }, o.label),
          h('span', { class: 'nas-desc md-typescale-body-small' }, o.hint)));
    }));

  const sampleItems = Array.isArray(p?.sample) ? p.sample : [];
  const sampleDetails = sampleItems.length
    ? h('details', { class: 'nas-details' },
      h('summary', { class: 'md-typescale-body-small' }, `Mostra anteprima (${sampleItems.length} file da copiare o in conflitto)`),
      h('ul', { class: 'nas-sample-list' },
        ...sampleItems.map((it) => h('li', { class: 'nas-sample-item' },
          h('span', { class: 'nas-sample-item__path', title: it.rel }, it.rel),
          pill(it.state === 'conflitto' ? 'Conflitto' : 'Da copiare', it.state === 'conflitto' ? 'down' : 'neutral')))))
    : null;

  const albums = Array.isArray(S.status?.albums) ? S.status.albums : [];
  let albumFilterBox = null;
  if (S.layout === 'by_album') {
    const modeAllChip = h('md-filter-chip', {
      label: 'Tutti gli album',
      onclick: async () => {
        if (!S.filterAlbums) return;
        S.filterAlbums = false;
        await loadPlan();
        render();
      },
    });
    modeAllChip.selected = !S.filterAlbums;

    const modePickChip = h('md-filter-chip', {
      label: 'Scegli quali inviare',
      onclick: async () => {
        if (S.filterAlbums) return;
        S.filterAlbums = true;
        if (!S.albumsInited) {
          S.albumsInited = true;
          albums.forEach((a) => S.selectedAlbumIds.add(a.id));
        }
        await loadPlan();
        render();
      },
    });
    modePickChip.selected = S.filterAlbums;

    let chipsGrid = null;
    if (S.filterAlbums) {
      const albumChips = albums.map((a) => {
        const c = h('md-filter-chip', {
          label: `${a.name} (${formatNumber(a.count)})`,
          onclick: async () => {
            if (S.selectedAlbumIds.has(a.id)) S.selectedAlbumIds.delete(a.id);
            else S.selectedAlbumIds.add(a.id);
            await loadPlan();
            render();
          },
        });
        c.selected = S.selectedAlbumIds.has(a.id);
        return c;
      });
      const unsortedChip = h('md-filter-chip', {
        label: 'Senza album',
        onclick: async () => {
          S.includeUnsorted = !S.includeUnsorted;
          await loadPlan();
          render();
        },
      });
      unsortedChip.selected = S.includeUnsorted;

      chipsGrid = h('div', { class: 'nas-album-picker__body' },
        h('div', { class: 'nas-album-picker__quick' },
          h('md-text-button', {
            onclick: async () => {
              albums.forEach((a) => S.selectedAlbumIds.add(a.id));
              S.includeUnsorted = true;
              await loadPlan();
              render();
            },
          }, 'Seleziona tutti'),
          h('md-text-button', {
            onclick: async () => {
              S.selectedAlbumIds.clear();
              S.includeUnsorted = false;
              await loadPlan();
              render();
            },
          }, 'Deseleziona tutti')),
        h('div', { class: 'nas-album-chips' }, ...albumChips, unsortedChip),
        (!S.selectedAlbumIds.size && !S.includeUnsorted)
          ? h('p', { class: 'nas-desc md-typescale-body-small' }, 'Seleziona almeno un album oppure «Senza album».')
          : null);
    }

    albumFilterBox = h('div', { class: 'nas-album-picker' },
      h('div', { class: 'nas-album-chips' }, modeAllChip, modePickChip),
      chipsGrid);
  }

  const emptySelection = S.layout === 'by_album' && S.filterAlbums && !S.selectedAlbumIds.size && !S.includeUnsorted;
  const sendBody = { layout: S.layout };
  if (S.layout === 'by_album' && S.filterAlbums) {
    sendBody.album_ids = [...S.selectedAlbumIds];
    sendBody.include_unsorted = S.includeUnsorted;
  }

  return h('section', { class: 'nas-card', 'aria-labelledby': 'nas-send-title' },
    h('div', { class: 'nas-card__head' },
      h('div', { class: 'nas-card__title-wrap' },
        h('span', { class: 'nas-card__icon', 'aria-hidden': 'true' }, icon('cloud_upload')),
        h('h2', { id: 'nas-send-title', class: 'md-typescale-title-medium' }, 'Invia al NAS'))),
    h('p', { class: 'nas-desc md-typescale-body-medium' },
      'Copia sul NAS le foto del catalogo e verifica l’hash di ogni file copiato. Non sovrascrive né cancella nulla.'),
    layoutGroup,
    albumFilterBox,
    noNas ? h('p', { class: 'nas-desc md-typescale-body-small' }, 'Configura prima la cartella del NAS qui sopra.') : null,
    p?.error ? h('div', { class: 'callout callout--error', role: 'alert' }, icon('error'), h('p', {}, p.error)) : null,
    p && !p.error ? h('div', { class: 'nas-plan-grid' },
      h('div', { class: 'nas-plan-stat' },
        h('span', { class: 'nas-plan-stat__num' }, formatNumber(counts.da_copiare || 0)),
        h('span', { class: 'nas-desc md-typescale-label-small' },
          p.bytes_to_copy ? `Da copiare (${formatBytes(p.bytes_to_copy)})` : 'Da copiare')),
      h('div', { class: 'nas-plan-stat nas-plan-stat--ok' },
        h('span', { class: 'nas-plan-stat__num' }, formatNumber(counts.presente || 0)),
        h('span', { class: 'nas-desc md-typescale-label-small' }, 'Già presenti')),
      h('div', { class: `nas-plan-stat${counts.conflitto ? ' nas-plan-stat--warn' : ''}` },
        h('span', { class: 'nas-plan-stat__num' }, formatNumber(counts.conflitto || 0)),
        h('span', { class: 'nas-desc md-typescale-label-small' }, 'Conflitti'))) : null,
    sampleDetails,
    h('div', { class: 'nas-actions' },
      h('md-filled-button', {
        disabled: noNas || unreachable || emptySelection || S.busy || running,
        onclick: startJob('/nas/send', sendBody),
      }, icon('cloud_upload', { slot: 'icon' }), 'Invia e verifica')));
}

// ---------- Sezione 4: Ricevi dal NAS ----------
function sectionReceive() {
  const noNas = !S.status?.nas_dir;
  const unreachable = !noNas && !S.status?.space?.reachable;
  const running = isJobRunning();

  const tf = document.createElement('md-outlined-text-field');
  tf.label = 'Cartella di destinazione sul PC';
  tf.placeholder = 'Es. C:\\Foto\\Dal NAS';
  tf.value = S.recvDraft ?? (S.status?.receive_dir || '');
  tf.supportingText = 'I file già presenti o con nome in conflitto non vengono mai sovrascritti.';

  const recvBtn = h('md-filled-tonal-button', {
    disabled: noNas || unreachable || !tf.value.trim() || S.busy || running,
    onclick: async () => {
      const dest = tf.value.trim();
      if (!dest) return;
      if (dest !== (S.status?.receive_dir || '')) {
        try {
          await api.put('/nas/settings', { nas_receive_dir: dest });
          if (S.status) S.status.receive_dir = dest;
        } catch (e) {
          showSnackbar(errText(e), { error: true });
          return;
        }
      }
      await startJob('/nas/receive', { dest })();
    },
  }, icon('cloud_download', { slot: 'icon' }), 'Ricevi dal NAS');

  const syncBtn = () => {
    recvBtn.disabled = noNas || unreachable || !tf.value.trim() || S.busy || running;
  };
  tf.addEventListener('input', () => { S.recvDraft = tf.value; syncBtn(); });
  tf.addEventListener('change', async () => {
    const val = tf.value.trim();
    if (val === (S.status?.receive_dir || '')) return;
    try {
      await api.put('/nas/settings', { nas_receive_dir: val });
      if (S.status) S.status.receive_dir = val;
      showSnackbar('Cartella di ricezione salvata.');
    } catch (e) {
      showSnackbar(errText(e), { error: true });
    }
  });

  const browseBtn = h('md-outlined-button', {
    onclick: () => pickFolderInto('Scegli dove salvare i file ricevuti dal NAS', tf.value.trim(), async (picked) => {
      tf.value = picked;
      S.recvDraft = picked;
      syncBtn();
      try {
        await api.put('/nas/settings', { nas_receive_dir: picked });
        if (S.status) S.status.receive_dir = picked;
        showSnackbar('Cartella di ricezione salvata.');
      } catch (e) {
        showSnackbar(errText(e), { error: true });
      }
    }),
  }, icon('folder_open', { slot: 'icon' }), 'Sfoglia…');

  return h('section', { class: 'nas-card', 'aria-labelledby': 'nas-recv-title' },
    h('div', { class: 'nas-card__head' },
      h('div', { class: 'nas-card__title-wrap' },
        h('span', { class: 'nas-card__icon', 'aria-hidden': 'true' }, icon('cloud_download')),
        h('h2', { id: 'nas-recv-title', class: 'md-typescale-title-medium' }, 'Ricevi dal NAS'))),
    h('p', { class: 'nas-desc md-typescale-body-medium' },
      'Scarica sul PC le foto e i video presenti sul NAS che non hai ancora in questa cartella, verificando l’integrità di ogni file.'),
    h('div', { class: 'nas-path-row' }, tf, browseBtn),
    h('div', { class: 'nas-actions' }, recvBtn));
}

// ---------- Sezione 5: Pulisci il PC (Cestino) ----------
function sectionCleanup() {
  const pv = S.preview || { count: 0, bytes: 0, sample: [] };
  const running = isJobRunning();

  const tf = document.createElement('md-outlined-text-field');
  tf.label = 'Scrivi CESTINO per confermare';
  tf.placeholder = 'CESTINO';
  tf.autocomplete = 'off';

  const btn = h('md-filled-button', {
    class: 'nas-btn-danger',
    disabled: true,
    onclick: async () => {
      if (tf.value.trim() !== 'CESTINO') return;
      S.busy = true;
      render();
      try {
        const j = await api.post('/nas/cleanup', { confirm: 'CESTINO', expected_count: pv.count });
        S.jobId = j.id;
        S.job = j;
        render();
      } catch (e) {
        showSnackbar(errText(e), { error: true });
        await loadPreview();
      } finally {
        S.busy = false;
        render();
      }
    },
  }, icon('delete', { slot: 'icon' }), `Sposta nel Cestino ${formatNumber(pv.count)} file`);

  tf.addEventListener('input', () => {
    btn.disabled = pv.count === 0 || S.busy || running || tf.value.trim() !== 'CESTINO';
  });

  return h('section', { class: `nas-card${pv.count > 0 ? ' nas-card--danger' : ''}`, 'aria-labelledby': 'nas-clean-title' },
    h('div', { class: 'nas-card__head' },
      h('div', { class: 'nas-card__title-wrap' },
        h('span', { class: `nas-card__icon ${pv.count > 0 ? 'nas-card__icon--danger' : 'nas-card__icon--warn'}`, 'aria-hidden': 'true' }, icon('delete_sweep')),
        h('h2', { id: 'nas-clean-title', class: 'md-typescale-title-medium' }, 'Pulisci il PC (Cestino)'))),
    h('p', { class: 'nas-desc md-typescale-body-medium' },
      'Sposta nel Cestino di Windows solo i file che risultano già copiati e verificati sul NAS con lo stesso hash, e che non sono cambiati sul PC. Puoi sempre recuperarli dal Cestino finché non lo svuoti.'),
    pv.count === 0
      ? h('div', { class: 'callout callout--info', role: 'status' },
        icon('verified'),
        h('div', {},
          h('p', { class: 'callout__title' }, 'Nessun file pronto per la pulizia'),
          h('p', {}, 'Prima esegui «Invia e verifica» al NAS: solo i file verificati compariranno qui.')))
      : h('div', { class: 'nas-cleanup-box' },
        h('p', { class: 'md-typescale-title-small', style: 'margin:0;' },
          `${plural(pv.count, 'file pronto', 'file pronti')} · ${formatBytes(pv.bytes)} liberabili`),
        pv.sample?.length
          ? h('p', { class: 'nas-desc md-typescale-body-small' },
            `Esempi: ${pv.sample.join(', ')}${pv.count > pv.sample.length ? '…' : ''}`)
          : null),
    pv.error ? h('div', { class: 'callout callout--error', role: 'alert' }, icon('error'), h('p', {}, pv.error)) : null,
    pv.count > 0 ? h('div', { class: 'nas-cleanup-confirm' }, tf, btn) : null);
}

// ---------- Sezione 6: Registro attività NAS ----------
function sectionLog() {
  if (!S.log.length) return null;
  return h('section', { class: 'nas-card', 'aria-labelledby': 'nas-log-title' },
    h('details', {},
      h('summary', { id: 'nas-log-title', class: 'md-typescale-title-small', style: 'cursor:pointer;' },
        `Registro attività NAS (${formatNumber(S.log.length)} voci recenti)`),
      h('div', { class: 'nas-log-list' },
        ...S.log.map((row) => {
          const meta = LOG_LABELS[row.action] || { text: row.action, cls: 'neutral' };
          return h('div', { class: 'nas-log-row' },
            h('span', { class: 'nas-log-row__ts' }, formatDateTime(row.ts)),
            pill(meta.text, meta.cls),
            h('span', { class: 'nas-log-row__detail' }, row.detail));
        }))));
}

function render() {
  if (!UI) return;
  const header = h('header', { class: 'view-header' },
    h('div', {},
      h('h1', { class: 'md-typescale-headline-medium' }, 'NAS'),
      h('p', { class: 'muted md-typescale-body-medium' },
        'Backup verificato con hash sul tuo disco di rete, ricezione sul PC e pulizia sicura nel Cestino.')),
    h('div', { class: 'view-header-actions' },
      h('md-outlined-button', {
        disabled: S.refreshing,
        onclick: () => refreshAll().catch((e) => showSnackbar(errText(e), { error: true })),
      }, icon('refresh', { slot: 'icon' }), 'Aggiorna')));

  const view = h('div', { class: 'view nas-view' },
    header,
    sectionOverview(),
    (UI.jobSlot = h('div', { class: 'nas-job-slot' }, sectionJob())),
    h('div', { class: 'nas-columns' }, sectionSend(), sectionReceive()),
    sectionCleanup(),
    sectionLog());
  UI.root.replaceChildren(view);
}

export async function mount(root, ctx) {
  loadCss();
  UI = { root, ctx };
  root.replaceChildren(h('div', { class: 'view nas-view' }, skeletonCard(), skeletonCard()));

  const off = onEvent('job', (j) => {
    if (!j || !String(j.name || '').startsWith('nas_')) return;
    if (S.jobId && j.id !== S.jobId && S.job?.state === 'running') return;
    // avanzamento dello stesso job gia' mostrato: si aggiorna solo il suo riquadro, cosi' percorsi,
    // cartelle scelte e la parola CESTINO scritta dall'utente non vanno persi a ogni evento
    const sameRunning = j.state === 'running' && S.job?.state === 'running' && S.job.id === j.id;
    S.jobId = j.id;
    S.job = j;
    if (sameRunning && UI?.jobSlot?.isConnected) {
      UI.jobSlot.replaceChildren(...[sectionJob()].filter(Boolean));
      return;
    }
    if (j.state !== 'running') {
      refreshAll().catch(() => {});
      showSnackbar(
        j.state === 'done' ? 'Operazione NAS completata.' : (j.error || j.message || 'Operazione NAS non completata.'),
        { error: j.state !== 'done' },
      );
    } else {
      render();
    }
  });

  try {
    await refreshAll();
  } catch (e) {
    if (e?.name === 'AbortError') return off;
    root.replaceChildren(errorState({ title: 'Impossibile aprire la pagina NAS', message: errText(e), onRetry: () => mount(root, ctx) }));
  }
  return () => { off(); UI = null; };
}
