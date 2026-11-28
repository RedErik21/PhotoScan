// Visualizzatore a schermo intero: overlay modale, NON una route (firma in docs/CONTRACT.md §3).
// Foto: zoom (rotella, doppio tocco, pizzico, pulsanti), pan, swipe con animazione, precaricamento.
// Video: <video controls> con Range. Pannello info, preferiti, album, apertura cartella.
import { api, fileUrl, ApiError } from '../api.js';
import { h, icon } from '../ui/el.js';
import { showSnackbar } from '../ui/snackbar.js';
import { albumPickerDialog, confirmDialog } from '../ui/dialog.js';
import { formatDateTime, formatBytes, formatDuration, formatNumber, missingNote } from '../ui/format.js';

const PRELOAD_AHEAD = 5;          // chiede loadMore() quando mancano meno di N foto
const MAX_TAIL_PAGES = 200;       // tetto di pagine caricate da "Fine" (200 x 120 foto)
const DOUBLE_TAP_MS = 300;
const DOUBLE_TAP_SCALE = 2.5;
const VIDEO_AUTOPLAY_MUTED = false; // autoplay muto dei video (opzionale)
const reduceMotion = window.matchMedia('(prefers-reduced-motion: reduce)');
const motionMs = () => (reduceMotion.matches ? 1 : parseFloat(getComputedStyle(document.documentElement).getPropertyValue('--dur-short')) || 150);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));
const errMsg = (e) => (e instanceof ApiError || e?.message ? e.message : 'Operazione non riuscita');

const SOURCE_LABEL = {
  exif: 'Dai dati EXIF della foto',
  sidecar: 'Dal file JSON di Google Foto',
  filename: 'Dal nome del file',
  video: 'Dai metadati del video',
  mtime: 'Data di modifica del file',
};

// Cartelle che il server apre in Esplora risorse (status.source_dir / output_dir). null = stato non disponibile.
let rootsPromise = null;
function loadRoots() {
  rootsPromise ||= api.get('/status')
    .then((s) => [s.source_dir, s.output_dir].filter(Boolean).map(normPath))
    .catch(() => null);
  return rootsPromise;
}
const normPath = (p) => String(p).replace(/\//g, '\\').replace(/\\+$/, '').toLowerCase();
function parentDir(path) {
  if (!path) return null;
  const i = Math.max(path.lastIndexOf('\\'), path.lastIndexOf('/'));
  return i > 0 ? path.slice(0, i) : null;
}

function ensureCss() {
  if (document.querySelector('link[data-viewer-css]')) return;
  document.head.append(h('link', { rel: 'stylesheet', href: 'css/view-viewer.css', dataset: { viewerCss: '' } }));
}

async function copyText(text) {
  try { await navigator.clipboard.writeText(text); return true; } catch { /* ripiego sotto */ }
  const ta = document.createElement('textarea');
  ta.value = text; ta.setAttribute('readonly', '');
  ta.style.position = 'fixed'; ta.style.opacity = '0';
  document.body.append(ta); ta.select();
  let ok = false;
  try { ok = document.execCommand('copy'); } finally { ta.remove(); }
  return ok;
}

/**
 * Apre il visualizzatore. items: File[] (copia interna, l'array dell'chiamante non viene modificato).
 * Ritorna { close(), goTo(i) }.
 */
export function openViewer({ items = [], index = 0, loadMore = null, onClose = null, onChange = null, onDelete = null } = {}) {
  if (!items.length) return null;
  ensureCss();

  const list = items.slice();
  const known = new Set(list.map((f) => f.id));
  let idx = clamp(index, 0, list.length - 1);
  let hasMore = typeof loadMore === 'function';
  let loadingMore = false;
  let loadError = false;     // ultimo caricamento fallito: il prefetch automatico si ferma
  let closed = false;
  let busy = false;          // animazione di scorrimento in corso
  let infoOpen = false;
  let chromeHidden = false;  // modalita' pulita senza barre (tocco singolo su foto)
  let folderRoots;           // undefined = in caricamento, null = non noto (mostra), array = cartelle consentite
  let attempt = 0;           // numero di nuovi tentativi per l'immagine corrente
  let lastTap = { t: 0, x: 0, y: 0 };
  let singleTapTimer = 0;
  let videoRate = 1;
  const data = { details: new Map(), albums: new Map() };
  const opener = document.activeElement;
  const prevBodyOverflow = document.body.style.overflow;
  const view = { scale: 1, tx: 0, ty: 0, sx: 0, rot: 0, ar: 1, nw: 0 }; // zoom, pan, swipe, rotazione, aspetto
  const pointers = new Map();
  let gesture = null;
  let canvas = null;
  let video = null;
  let fullImg = null;
  const menuAnchorId = `viewer-menu-${Math.random().toString(36).slice(2, 8)}`;

  const cur = () => list[idx];
  const isVideo = () => cur().kind === 'video';
  const dialogOpen = () => !!document.querySelector('md-dialog[open]');
  const setShown = (el, on) => { el.style.display = on ? '' : 'none'; };

  // ---------- DOM ----------
  const iconBtn = (name, label, onClick) =>
    h('md-icon-button', { 'aria-label': label, title: label, onclick: onClick }, icon(name));
  const menuItem = (name, label, onClick) =>
    h('md-menu-item', { onclick: onClick }, icon(name, { slot: 'start' }), h('div', { slot: 'headline' }, label));

  const titleName = h('span', { class: 'viewer-topbar__name' });
  const titleSub = h('span', { class: 'viewer-topbar__sub' });
  const titleBox = h('div', { class: 'viewer-topbar__title' }, titleName, titleSub);
  const btnClose = iconBtn('close', 'Chiudi', () => close());
  const btnFav = h('md-icon-button', { toggle: true, 'aria-label': 'Preferito', title: 'Preferito (F)', onclick: () => toggleFavorite() },
    icon('favorite'), icon('favorite', { filled: true, slot: 'selected' }));
  // Su telefono (<600px) album e informazioni passano nel menu "Altre azioni" per lasciare spazio al titolo.
  const btnAlbum = h('md-icon-button', { class: 'viewer-topbar__wide', 'aria-label': 'Aggiungi ad album (A)', title: 'Aggiungi ad album (A)', onclick: () => addToAlbum() }, icon('library_add'));
  const btnInfo = h('md-icon-button', { class: 'viewer-topbar__wide', toggle: true, 'aria-label': 'Informazioni', title: 'Informazioni (I)', onclick: () => toggleInfo() },
    icon('info'), icon('info', { filled: true, slot: 'selected' }));
  const btnMenu = h('md-icon-button', { id: `${menuAnchorId}-btn`, 'aria-label': 'Altre azioni', title: 'Altre azioni' }, icon('more_vert'));
  const itemAlbum = menuItem('library_add', 'Aggiungi ad album', () => addToAlbum());
  itemAlbum.classList.add('viewer-menu-compact');
  const itemFolder = menuItem('folder_open', 'Apri posizione del file', () => openFolder());
  const itemCopy = menuItem('content_copy', 'Copia percorso', () => copyPath());
  const itemInfo = menuItem('info', 'Informazioni', () => toggleInfo());
  const itemDelete = menuItem('delete', 'Sposta nel Cestino', () => deleteCurrent());
  itemDelete.classList.add('viewer-menu-danger');
  const menu = h('md-menu', { anchor: `${menuAnchorId}-btn`, positioning: 'popover' },
    itemAlbum, itemFolder, itemCopy, itemInfo,
    h('md-divider', { role: 'separator' }),
    itemDelete);
  btnMenu.addEventListener('click', () => { menu.open = !menu.open; });
  const btnFs = iconBtn('fullscreen', 'Schermo intero', () => toggleFullscreen());
  const topbar = h('header', { class: 'viewer-topbar' },
    btnClose, titleBox,
    h('div', { class: 'viewer-topbar__actions' }, btnFav, btnAlbum, btnFs, btnInfo, btnMenu, menu));

  const btnPrev = h('md-icon-button', { class: 'viewer-nav viewer-nav--prev', 'aria-label': 'Foto precedente', title: 'Precedente (←)', onclick: () => step(-1) }, icon('chevron_left'));
  const btnNext = h('md-icon-button', { class: 'viewer-nav viewer-nav--next', 'aria-label': 'Foto successiva', title: 'Successiva (→)', onclick: () => step(1) }, icon('chevron_right'));

  const zoomLabel = h('button', {
    type: 'button', class: 'viewer-zoom__label',
    title: 'Clicca per tornare a 100% / Adatta',
    onclick: () => (view.scale > 1.05 ? resetZoom() : toggleZoomAt(window.innerWidth / 2, window.innerHeight / 2)),
  }, 'Adatta');
  const zoomBar = h('div', { class: 'viewer-zoom', role: 'toolbar', 'aria-label': 'Controlli foto' },
    iconBtn('zoom_out', 'Riduci zoom (-)', () => zoomBy(1 / 1.5)),
    zoomLabel,
    iconBtn('zoom_in', 'Ingrandisci (+)', () => zoomBy(1.5)),
    h('span', { class: 'viewer-zoom__sep', 'aria-hidden': 'true' }),
    iconBtn('rotate_right', 'Ruota anteprima 90° (R)', () => rotateBy(90)),
    iconBtn('fit_screen', 'Adatta alla finestra (0)', () => resetZoom()));

  const rateBtn = h('button', {
    type: 'button', class: 'viewer-zoom__label',
    title: 'Velocità di riproduzione',
    onclick: () => cycleVideoRate(),
  }, '1×');
  const videoBar = h('div', { class: 'viewer-zoom viewer-video-bar', role: 'toolbar', 'aria-label': 'Controlli video', hidden: true },
    iconBtn('replay_10', 'Indietro 10 secondi (J)', () => seekVideo(-10)),
    iconBtn('forward_10', 'Avanti 10 secondi (L)', () => seekVideo(10)),
    h('span', { class: 'viewer-zoom__sep', 'aria-hidden': 'true' }),
    rateBtn);

  const loader = h('md-circular-progress', { class: 'viewer-loader', indeterminate: true, hidden: true, 'aria-label': 'Caricamento altre foto' });
  const stage = h('div', { class: 'viewer-stage' });
  const info = h('aside', { class: 'viewer-info', hidden: true, 'aria-label': 'Informazioni sul file' });
  const overlay = h('div', { class: 'viewer-overlay', role: 'dialog', 'aria-modal': 'true', 'aria-label': 'Visualizzatore' },
    topbar, stage, btnPrev, btnNext, zoomBar, videoBar, loader, info);

  // ---------- stato visivo ----------
  function setFav(on) { btnFav.selected = !!on; }

  function updateChrome() {
    if (closed) return;
    const f = cur();
    titleName.textContent = f.name;
    titleName.title = f.name;
    titleSub.textContent = `${formatDateTime(f.date)} · ${idx + 1} di ${list.length}${hasMore ? '+' : ''}`;
    setFav(f.favorite);
    btnInfo.selected = infoOpen;
    btnPrev.disabled = idx === 0;
    btnNext.disabled = idx >= list.length - 1 && (!hasMore || loadingMore);
    loader.hidden = !loadingMore;
    zoomBar.hidden = isVideo();
    videoBar.hidden = !isVideo();
    setShown(itemFolder, canOpenFolder(f));
    const fsActive = document.fullscreenElement === overlay;
    const fsIcon = btnFs.querySelector('.material-symbols-outlined');
    if (fsIcon) fsIcon.textContent = fsActive ? 'fullscreen_exit' : 'fullscreen';
    btnFs.title = fsActive ? 'Esci da schermo intero' : 'Schermo intero';
  }

  function canOpenFolder(f) {
    const parent = parentDir(f.path);
    if (!parent || folderRoots === undefined) return false;
    if (folderRoots === null) return true;
    const p = normPath(parent);
    return folderRoots.some((r) => p === r || p.startsWith(r + '\\'));
  }

  // ---------- zoom e pan (solo foto) ----------
  // Dimensioni della foto "adattata" dentro lo stage (object-fit: contain).
  function fit() {
    const W = stage.clientWidth, H = stage.clientHeight;
    const ar = view.ar || 1;
    let fw = W, fh = W / ar;
    if (fh > H) { fh = H; fw = H * ar; }
    return { W, H, fw, fh };
  }
  const maxScale = () => {
    const { fw } = fit();
    const native = view.nw && fw ? view.nw / fw : 1; // 1:1 con i pixel originali
    return clamp(native, 3, 12);
  };
  function clampPan() {
    const { W, H, fw, fh } = fit();
    const mx = Math.max(0, (fw * view.scale - W) / 2);
    const my = Math.max(0, (fh * view.scale - H) / 2);
    view.tx = clamp(view.tx, -mx, mx);
    view.ty = clamp(view.ty, -my, my);
  }
  function applyView() {
    if (canvas) {
      const rot = view.rot ? ` rotate(${view.rot}deg)` : '';
      canvas.style.transform = `translate3d(${view.tx + view.sx}px, ${view.ty}px, 0) scale(${view.scale})${rot}`;
      canvas.classList.toggle('is-zoomed', view.scale > 1.001);
    }
    zoomLabel.textContent = zoomText();
  }
  function zoomText() {
    if (view.scale <= 1.001) return 'Adatta';
    const { fw } = fit();
    const pct = view.nw && fw ? (fw * view.scale / view.nw) * 100 : view.scale * 100;
    return `${Math.round(pct)}%`;
  }
  function resetView() { view.scale = 1; view.tx = 0; view.ty = 0; view.sx = 0; view.rot = 0; view.nw = 0; view.ar = 1; }
  function rotateBy(deg) {
    if (!canvas) return;
    setAnim(motionMs());
    view.rot = (view.rot + deg) % 360;
    applyView();
    setTimeout(() => setAnim(0), motionMs() + 20);
  }
  function toggleChrome(force) {
    if (isVideo() || infoOpen) return;
    chromeHidden = typeof force === 'boolean' ? force : !chromeHidden;
    overlay.classList.toggle('is-immersive', chromeHidden);
  }
  function toggleFullscreen() {
    if (!document.fullscreenEnabled) return;
    if (document.fullscreenElement === overlay) document.exitFullscreen().catch(() => {});
    else overlay.requestFullscreen().catch(() => {});
  }
  function seekVideo(sec) {
    if (!video || !Number.isFinite(video.duration)) return;
    video.currentTime = clamp(video.currentTime + sec, 0, video.duration);
  }
  function cycleVideoRate() {
    const rates = [0.5, 1, 1.25, 1.5, 2];
    videoRate = rates[(rates.indexOf(videoRate) + 1) % rates.length];
    if (video) video.playbackRate = videoRate;
    rateBtn.textContent = `${String(videoRate).replace('.', ',')}×`;
  }
  function releaseVideo() {
    if (!video) return;
    try {
      video.pause();
      video.removeAttribute('src');
      video.load();
    } catch { /* ignora */ }
    video = null;
  }
  // Coordinate del puntatore relative al centro dello stage.
  function relCenter(x, y) {
    const r = stage.getBoundingClientRect();
    return { px: x - r.left - r.width / 2, py: y - r.top - r.height / 2 };
  }
  function zoomAt(ns, px = 0, py = 0) {
    if (!canvas) return;
    const s0 = view.scale;
    ns = clamp(ns, 1, maxScale());
    view.tx = px - (px - view.tx) * (ns / s0);
    view.ty = py - (py - view.ty) * (ns / s0);
    view.scale = ns;
    clampPan();
    applyView();
  }
  function setAnim(ms) {
    if (canvas) canvas.style.transition = ms ? `transform ${ms}ms var(--motion-standard)` : 'none';
  }
  function zoomBy(factor) {
    if (!canvas) return;
    setAnim(motionMs());
    zoomAt(view.scale * factor);
    setTimeout(() => setAnim(0), motionMs() + 20);
  }
  function resetZoom() {
    if (!canvas) return;
    setAnim(motionMs());
    view.scale = 1; view.tx = 0; view.ty = 0;
    applyView();
    setTimeout(() => setAnim(0), motionMs() + 20);
  }
  function toggleZoomAt(x, y) {
    if (!canvas) return;
    if (view.scale > 1.05) { resetZoom(); return; }
    const { px, py } = relCenter(x, y);
    setAnim(motionMs());
    zoomAt(DOUBLE_TAP_SCALE, px, py);
    setTimeout(() => setAnim(0), motionMs() + 20);
  }
  // Animazione orizzontale (swipe); risolve a fine animazione.
  async function slideTo(sx) {
    if (!canvas) return;
    setAnim(motionMs());
    view.sx = sx;
    applyView();
    await sleep(motionMs());
    setAnim(0);
  }

  // ---------- gesti ----------
  function onPointerDown(e) {
    if (!canvas || busy || closed) return;
    // Sopra un controllo (es. "Riprova") niente cattura del puntatore: altrimenti il click viene ri-targettato sullo stage.
    if (e.target.closest?.('button, md-icon-button, md-filled-tonal-button, md-text-button, .viewer-error')) return;
    pointers.set(e.pointerId, { x: e.clientX, y: e.clientY });
    try { stage.setPointerCapture(e.pointerId); } catch { /* ignora */ }
    setAnim(0);
    if (pointers.size === 1) {
      gesture = { mode: 'press', x: e.clientX, y: e.clientY, tx: view.tx, ty: view.ty, t: performance.now(), moved: false, dx: 0 };
    } else if (pointers.size === 2) {
      const [a, b] = [...pointers.values()];
      gesture = { mode: 'pinch', d0: Math.hypot(a.x - b.x, a.y - b.y), s0: view.scale };
    }
  }
  function onPointerMove(e) {
    if (!pointers.has(e.pointerId)) return;
    pointers.set(e.pointerId, { x: e.clientX, y: e.clientY });
    const g = gesture;
    if (!g) return;
    if (g.mode === 'pinch' && pointers.size >= 2) {
      const [a, b] = [...pointers.values()];
      const m = relCenter((a.x + b.x) / 2, (a.y + b.y) / 2);
      zoomAt(g.s0 * Math.hypot(a.x - b.x, a.y - b.y) / g.d0, m.px, m.py);
      return;
    }
    if (g.mode !== 'press' && g.mode !== 'pan' && g.mode !== 'swipe') return;
    const dx = e.clientX - g.x, dy = e.clientY - g.y;
    if (g.mode === 'press') {
      if (Math.hypot(dx, dy) <= 6) return;
      g.moved = true;
      if (view.scale > 1.001) g.mode = 'pan';
      else if (Math.abs(dx) > Math.abs(dy)) g.mode = 'swipe';
      else return;
    }
    if (g.mode === 'pan') {
      view.tx = g.tx + dx; view.ty = g.ty + dy;
      clampPan(); applyView();
    } else if (g.mode === 'swipe') {
      g.dx = dx; view.sx = dx; applyView();
    }
  }
  function onPointerUp(e) {
    if (!pointers.has(e.pointerId)) return;
    pointers.delete(e.pointerId);
    const g = gesture;
    if (!g) return;
    if (g.mode === 'pinch') {
      gesture = null;
      if (view.scale < 1.02) { resetZoom(); return; }
      // Un dito e' ancora appoggiato: da qui in poi e' un pan, senza doverlo risollevare.
      const rest = pointers.size === 1 ? [...pointers.values()][0] : null;
      if (rest) gesture = { mode: 'pan', x: rest.x, y: rest.y, tx: view.tx, ty: view.ty, t: performance.now(), moved: true, dx: 0 };
      return;
    }
    if (pointers.size > 0) return;
    gesture = null;
    if (g.mode === 'swipe') {
      const dx = g.dx;
      const fast = Math.abs(dx) / Math.max(1, performance.now() - g.t) > 0.5 && Math.abs(dx) > 24;
      const far = Math.abs(dx) > stage.clientWidth * 0.2;
      const delta = far || fast ? (dx < 0 ? 1 : -1) : 0;
      if (delta) step(delta, dx); else slideTo(0);
      return;
    }
    if (g.mode === 'press' && !g.moved) {
      const now = performance.now();
      if (now - lastTap.t < DOUBLE_TAP_MS && Math.hypot(e.clientX - lastTap.x, e.clientY - lastTap.y) < 30) {
        clearTimeout(singleTapTimer);
        lastTap = { t: 0, x: 0, y: 0 };
        toggleZoomAt(e.clientX, e.clientY);
      } else {
        lastTap = { t: now, x: e.clientX, y: e.clientY };
        clearTimeout(singleTapTimer);
        singleTapTimer = setTimeout(() => { if (!closed) toggleChrome(); }, DOUBLE_TAP_MS);
      }
    }
  }
  function onPointerCancel() {
    const g = gesture;
    pointers.clear();
    gesture = null;
    if (g && g.mode === 'swipe') slideTo(0);
  }
  function onWheel(e) {
    if (!canvas) return;
    e.preventDefault();
    const { px, py } = relCenter(e.clientX, e.clientY);
    zoomAt(view.scale * Math.exp(-e.deltaY * 0.0015), px, py);
  }

  // ---------- contenuto dello stage ----------
  function showError(message) {
    stage.querySelector('.viewer-error')?.remove();
    stage.append(h('div', { class: 'viewer-error', role: 'alert' },
      icon('broken_image'),
      h('p', { class: 'viewer-error__text' }, message),
      h('md-filled-tonal-button', { onclick: () => { attempt += 1; renderStage(0); } }, 'Riprova')));
  }

  function renderStage(enter = 0) {
    const f = cur();
    const bust = attempt ? `&r=${attempt}` : '';
    releaseVideo();
    stage.replaceChildren();
    canvas = null; fullImg = null;
    resetView();
    if (f.kind === 'video') {
      toggleChrome(false);
      video = h('video', {
        class: 'viewer-video', controls: true, preload: 'metadata', playsinline: true,
        poster: fileUrl.thumb(f.id, 512), src: fileUrl.media(f.id) + bust,
        autoplay: VIDEO_AUTOPLAY_MUTED, muted: VIDEO_AUTOPLAY_MUTED, 'aria-label': f.name,
      });
      video.playbackRate = videoRate;
      video.addEventListener('error', () => { if (cur() === f) showError('Video non disponibile.'); });
      stage.append(h('div', { class: 'viewer-video-wrap' }, video));
      applyView();
      return;
    }
    canvas = h('div', { class: 'viewer-canvas' });
    const thumb = h('img', { class: 'viewer-thumb', src: fileUrl.thumb(f.id, 512), alt: '', decoding: 'async' });
    const imgLoader = h('md-circular-progress', { class: 'viewer-img-loader', indeterminate: true, 'aria-label': 'Caricamento immagine' });
    fullImg = h('img', { class: 'viewer-full', alt: f.name, decoding: 'async' });
    const img = fullImg;
    img.addEventListener('load', () => {
      if (img !== fullImg) return;
      thumb.remove(); // la miniatura sfocata serve solo finche' la foto intera non e' pronta
      imgLoader.remove();
      view.nw = img.naturalWidth;
      view.ar = img.naturalWidth / img.naturalHeight || 1;
      img.classList.add('is-loaded');
      clampPan(); applyView();
    });
    img.addEventListener('error', () => {
      imgLoader.remove();
      if (img === fullImg) showError('Immagine non disponibile.');
    });
    img.src = fileUrl.display(f.id) + bust;
    canvas.append(thumb, img);
    stage.append(canvas, imgLoader);
    if (enter) {
      view.sx = enter * stage.clientWidth;
      applyView();
      void canvas.offsetWidth;
      slideTo(0);
    }
    applyView();
  }

  function preload() {
    for (const j of [idx - 1, idx + 1]) {
      const f = list[j];
      if (f && f.kind !== 'video') { const im = new Image(); im.src = fileUrl.display(f.id); }
    }
  }

  // Passa alla foto i. enter = direzione da cui entra (-1/+1) per l'animazione.
  function go(i, enter = 0) {
    if (closed || i < 0 || i >= list.length) return;
    idx = i;
    attempt = 0;
    renderStage(enter);
    onChange?.(cur());
    updateChrome();
    if (infoOpen) { loadInfoData(cur()); renderInfo(); }
    preload();
    maybeFetchMore();
  }

  /** Fine: carica tutte le pagine rimanenti (fino a un tetto di sicurezza) e va all'ultimo elemento. */
  async function goLast() {
    if (closed || busy) return;
    busy = true;
    try {
      for (let n = 0; hasMore && n < MAX_TAIL_PAGES && !closed; n++) {
        if (!(await fetchMore())) break;
      }
      if (!closed) go(list.length - 1);
    } finally { busy = false; }
  }

  async function step(delta, fromDx = 0) {
    if (closed || busy) return;
    const target = idx + delta;
    if (target < 0) { slideTo(0); return; }
    busy = true; // prima di qualunque await: niente scorrimenti concorrenti
    try {
      if (target >= list.length) {
        loadError = false; // l'utente ha chiesto esplicitamente la foto successiva: nuovo tentativo
        await fetchMore();
        if (closed || target >= list.length) { await slideTo(0); return; }
      }
      if (!canvas) { go(target, 0); return; }
      if (fromDx === 0 && view.scale > 1.001) { go(target, 0); return; }
      await slideTo(-delta * stage.clientWidth);
      if (closed) return;
      go(target, delta);
    } finally { busy = false; }
  }

  // ---------- caricamento successivo ----------
  async function fetchMore() {
    if (!hasMore || loadingMore || !loadMore) return false;
    loadingMore = true;
    if (!closed) updateChrome();
    try {
      const extra = (await loadMore()) || [];
      const fresh = extra.filter((f) => !known.has(f.id));
      fresh.forEach((f) => known.add(f.id));
      list.push(...fresh);
      if (!extra.length) hasMore = false;
      return fresh.length > 0;
    } catch (e) {
      loadError = true; // niente nuovi tentativi automatici finche' l'utente non chiede la foto successiva
      showSnackbar(errMsg(e) || 'Impossibile caricare altre foto', { error: true });
      return false;
    } finally {
      loadingMore = false;
      if (!closed) updateChrome();
    }
  }
  function maybeFetchMore() {
    if (hasMore && !loadingMore && !loadError && idx >= list.length - PRELOAD_AHEAD) fetchMore();
  }

  // ---------- azioni ----------
  async function toggleFavorite() {
    const f = cur();
    if (f.favPending) return; // richiesta precedente ancora in corso: ignora il tasto ripetuto
    const want = !f.favorite;
    f.favorite = want;          // aggiornamento ottimistico: la pressione successiva parte dallo stato mostrato
    f.favPending = true;
    if (cur() === f) setFav(want);
    try {
      const r = await api.post(`/files/${f.id}/favorite`, { value: want });
      f.favorite = !!r.favorite;
    } catch (e) {
      f.favorite = !want;
      showSnackbar(errMsg(e), { error: true });
    } finally {
      f.favPending = false;
    }
    if (cur() === f) setFav(f.favorite);
    if (!closed) onChange?.(f); // la griglia sotto riflette il nuovo stato
  }

  async function addToAlbum() {
    const f = cur();
    let changed = false;
    try { changed = await albumPickerDialog({ fileIds: [f.id] }); } catch (e) { showSnackbar(errMsg(e), { error: true }); }
    if (changed) {
      data.albums.delete(f.id);
      if (infoOpen && cur() === f) { loadInfoData(f); renderInfo(); }
    }
  }

  async function openFolder() {
    const parent = parentDir(cur().path);
    if (!parent) return;
    try { await api.post('/system/open-folder', { path: parent }); }
    catch (e) { showSnackbar(errMsg(e), { error: true }); }
  }

  async function copyPath() {
    if (!cur().path) { showSnackbar('Percorso non disponibile', { error: true }); return; }
    const ok = await copyText(cur().path);
    showSnackbar(ok ? 'Percorso copiato' : 'Impossibile copiare il percorso', { error: !ok });
  }

  async function deleteCurrent() {
    const f = cur();
    if (!f || busy) return;
    const isVid = f.kind === 'video';
    const ok = await confirmDialog({
      title: `Spostare ${isVid ? 'questo video' : 'questa foto'} nel Cestino?`,
      message: `«${f.name}» verrà spostato nel Cestino di Windows e rimosso dal catalogo. Potrai sempre recuperarlo dal Cestino.`,
      confirmLabel: 'Sposta nel Cestino',
      danger: true,
    });
    if (!ok || closed) return;
    busy = true;
    // Su Windows il video aperto in streaming blocca il file: rilasciamo lo stream prima di spostarlo nel Cestino.
    if (isVid) releaseVideo();
    try {
      const res = await api.post('/cleanup/trash', { file_ids: [f.id], confirm: true });
      if (closed) return;
      if (!res.trashed && !res.missing) {
        showSnackbar(res.errors?.[0]?.error || 'Impossibile spostare nel Cestino.', { error: true });
        if (isVid) renderStage(0);
        return;
      }
      showSnackbar(res.trashed ? 'Spostato nel Cestino.' : missingNote(res.missing));
      const removedIndex = list.indexOf(f);
      if (removedIndex >= 0) list.splice(removedIndex, 1);
      known.delete(f.id);
      data.details.delete(f.id);
      data.albums.delete(f.id);
      onDelete?.(f);
      window.dispatchEvent(new CustomEvent('fs:file-deleted', { detail: { id: f.id } }));
      if (!list.length) {
        close();
        return;
      }
      const nextIdx = Math.min(idx, list.length - 1);
      busy = false;
      go(nextIdx, 0);
      return;
    } catch (e) {
      if (!closed) {
        showSnackbar(errMsg(e), { error: true });
        if (isVid) renderStage(0);
      }
    } finally {
      busy = false;
    }
  }

  function toggleInfo(force) {
    infoOpen = typeof force === 'boolean' ? force : !infoOpen;
    info.hidden = !infoOpen;
    overlay.classList.toggle('has-info', infoOpen);
    updateChrome();
    if (infoOpen) { loadInfoData(cur()); renderInfo(); }
  }

  // ---------- pannello info ----------
  function loadInfoData(f) {
    const refresh = () => { if (infoOpen && !closed && cur() === f) renderInfo(); };
    if (!data.details.has(f.id)) {
      api.get(`/files/${f.id}`)
        .then((d) => { data.details.set(f.id, d); refresh(); })
        .catch((e) => { data.details.set(f.id, null); showSnackbar(errMsg(e), { error: true }); refresh(); });
    }
    if (!data.albums.has(f.id)) {
      api.get(`/files/${f.id}/albums`)
        .then((a) => { data.albums.set(f.id, a || []); refresh(); })
        .catch((e) => { data.albums.set(f.id, null); showSnackbar(errMsg(e), { error: true }); refresh(); });
    }
  }

  function renderInfo() {
    if (!infoOpen || closed) return;
    const f = cur();
    const det = data.details.get(f.id);   // undefined = in caricamento, null = errore
    const alb = data.albums.get(f.id);
    const row = (label, ...value) => h('div', { class: 'viewer-info__row' }, h('dt', {}, label), h('dd', {}, ...value));

    const dateValue = [formatDateTime(f.date)];
    const origin = SOURCE_LABEL[f.date_source] || 'Origine sconosciuta';
    dateValue.push(h('br'), h('span', { class: 'viewer-info__sub' }, origin));
    if (f.date_uncertain) dateValue.push(h('br'), h('span', { class: 'viewer-info__warn' }, 'Data incerta'));

    const gps = f.lat != null && f.lon != null
      ? h('a', { href: `https://www.openstreetmap.org/?mlat=${f.lat}&mlon=${f.lon}#map=15/${f.lat}/${f.lon}`, target: '_blank', rel: 'noopener noreferrer' },
        `${f.lat.toFixed(4)}, ${f.lon.toFixed(4)} (OpenStreetMap)`)
      : '—';

    const dupText = f.is_dup === 1 ? 'Copia esatta di un altro file'
      : f.is_dup === 2 ? 'Quasi uguale a un altro file' : 'No';
    const dupValue = [dupText];
    if (f.is_dup) {
      dupValue.push(' · ', h('a', { href: '#/duplicates' }, 'Vedi nei duplicati'));
    }
    if (det && det.group && det.group.length > 1) dupValue.push(h('br'), h('span', { class: 'viewer-info__sub' }, `${det.group.length} file con lo stesso contenuto`));

    const lists = [
      row('Data', ...dateValue),
      row('Dimensioni', f.width && f.height ? `${formatNumber(f.width)} × ${formatNumber(f.height)} px` : '—'),
      row('Peso', formatBytes(f.size)),
    ];
    if (f.kind === 'video') lists.push(row('Durata', formatDuration(f.duration) || '—'));
    if (det && det.exif_summary && det.exif_summary.camera) lists.push(row('Fotocamera', det.exif_summary.camera));
    lists.push(row('Posizione', gps));
    lists.push(row('Contesto', f.context
      ? `${f.context}${f.score != null ? ` (punteggio ${Math.round(f.score * 100)}%)` : ''}` : '—'));
    lists.push(row('Duplicato', ...dupValue));

    const albumsEl = h('div', { class: 'viewer-info__chips' });
    if (alb === undefined) albumsEl.append(h('span', { class: 'viewer-info__sub' }, 'Caricamento…'));
    else if (alb === null) albumsEl.append(h('span', { class: 'viewer-info__sub' }, 'Non disponibile'));
    else if (!alb.length) albumsEl.append(h('span', { class: 'viewer-info__sub' }, 'Nessun album'));
    else alb.forEach((a) => albumsEl.append(h('a', { class: 'viewer-info__chip', href: `#/albums/${a.id}` }, a.name)));

    const btnOpen = h('md-text-button', { onclick: () => openFolder(), style: { display: canOpenFolder(f) ? '' : 'none' } },
      icon('folder_open', { slot: 'icon' }), 'Apri cartella');

    info.replaceChildren(
      h('div', { class: 'viewer-info__header' },
        h('h2', { class: 'viewer-info__title' }, 'Informazioni'),
        h('md-icon-button', { 'aria-label': 'Chiudi informazioni', title: 'Chiudi informazioni', onclick: () => toggleInfo(false) }, icon('close'))),
      h('p', { class: 'viewer-info__name' }, f.name),
      h('dl', { class: 'viewer-info__list' }, ...lists),
      h('section', { class: 'viewer-info__section' },
        h('h3', { class: 'viewer-info__label' }, 'Percorso'),
        h('code', { class: 'viewer-info__path' }, f.path || '—'),
        h('div', { class: 'viewer-info__actions' },
          h('md-text-button', { onclick: () => copyPath() }, icon('content_copy', { slot: 'icon' }), 'Copia percorso'),
          btnOpen)),
      h('section', { class: 'viewer-info__section' },
        h('h3', { class: 'viewer-info__label' }, 'Album'),
        albumsEl));
  }

  // ---------- chiusura, focus, tastiera ----------
  const FOCUSABLE = 'md-icon-button, md-text-button, md-filled-tonal-button, md-menu-item, a[href], video[controls]';
  // I componenti md-* delegano il focus al <button> interno (shadow DOM).
  function focusEl(el) {
    if (!el) return;
    el.focus();
    if (document.activeElement !== el && el.shadowRoot) {
      el.shadowRoot.querySelector('button, a, [tabindex]:not([tabindex="-1"])')?.focus();
    }
  }
  function focusables() {
    return [...overlay.querySelectorAll(FOCUSABLE)].filter((el) => el.getClientRects().length > 0);
  }
  function trapTab(e) {
    const f = focusables();
    if (!f.length) return;
    const first = f[0], last = f[f.length - 1];
    const a = document.activeElement;
    const inside = overlay.contains(a);
    if (e.shiftKey && (!inside || a === first)) { e.preventDefault(); focusEl(last); }
    else if (!e.shiftKey && (!inside || a === last)) { e.preventDefault(); focusEl(first); }
  }
  function onKey(e) {
    if (closed || dialogOpen() || menu.open) return;
    if (e.ctrlKey || e.metaKey || e.altKey) return;
    if (e.key === 'Tab') { trapTab(e); return; }
    const t = e.target;
    if (t && (t.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName))) return;
    switch (e.key) {
      case 'Escape':
        if (infoOpen) toggleInfo(false); else close(); // prima chiude il pannello, poi la foto
        break;
      case 'ArrowLeft': step(-1); break;
      case 'ArrowRight': step(1); break;
      case 'Home': if (!busy) go(0); break;
      case 'End': goLast(); break;
      case '+': case '=': zoomBy(1.25); break;
      case '-': case '_': zoomBy(1 / 1.25); break;
      case '0': resetZoom(); break;
      case 'r': case 'R': rotateBy(90); break;
      case ' ': case 'k': case 'K':
        if (video) { if (video.paused) video.play().catch(() => {}); else video.pause(); } else return;
        break;
      case 'j': case 'J': if (video) seekVideo(-10); else return; break;
      case 'l': case 'L': if (video) seekVideo(10); else return; break;
      case 'm': case 'M': if (video) video.muted = !video.muted; else return; break;
      case 'Delete': deleteCurrent(); break;
      case 'i': case 'I': toggleInfo(); break;
      case 'f': case 'F': toggleFavorite(); break;
      case 'a': case 'A': addToAlbum(); break;
      default: return;
    }
    e.preventDefault();
  }
  function onFocusIn(e) {
    if (closed || dialogOpen() || overlay.contains(e.target)) return;
    focusEl(btnClose);
  }
  function onResize() { clampPan(); applyView(); }
  function onFullscreenChange() { updateChrome(); }

  function close() {
    if (closed) return;
    closed = true;
    clearTimeout(singleTapTimer);
    document.removeEventListener('keydown', onKey, true);
    document.removeEventListener('focusin', onFocusIn, true);
    document.removeEventListener('fullscreenchange', onFullscreenChange);
    window.removeEventListener('hashchange', close);
    window.removeEventListener('resize', onResize);
    if (document.fullscreenElement === overlay) document.exitFullscreen().catch(() => {});
    releaseVideo();
    // Dissolvenza di uscita (view-viewer.css); il nodo viene tolto a fine animazione.
    overlay.style.pointerEvents = 'none';
    overlay.classList.add('is-closing');
    setTimeout(() => overlay.remove(), motionMs() + 20);
    document.body.style.overflow = prevBodyOverflow;
    if (opener && opener.isConnected && typeof opener.focus === 'function') opener.focus();
    onClose?.();
  }

  // ---------- avvio ----------
  stage.addEventListener('pointerdown', onPointerDown);
  stage.addEventListener('pointermove', onPointerMove);
  stage.addEventListener('pointerup', onPointerUp);
  stage.addEventListener('pointercancel', onPointerCancel);
  stage.addEventListener('wheel', onWheel, { passive: false });
  // Sulla foto il clic non sposta il focus fuori dal visualizzatore (il video ha i suoi controlli nativi).
  stage.addEventListener('mousedown', (e) => { if (canvas) e.preventDefault(); });
  document.body.append(overlay);
  document.body.style.overflow = 'hidden';
  document.addEventListener('keydown', onKey, true);
  document.addEventListener('focusin', onFocusIn, true);
  document.addEventListener('fullscreenchange', onFullscreenChange);
  window.addEventListener('hashchange', close);
  window.addEventListener('resize', onResize);
  loadRoots().then((r) => { folderRoots = r; if (!closed) updateChrome(); });

  renderStage(0);
  updateChrome();
  preload();
  maybeFetchMore();
  focusEl(btnClose);
  // Se il componente md-* non e' ancora registrato, il focus va messo appena lo e'.
  customElements.whenDefined('md-icon-button').then(() => {
    if (!closed && !overlay.contains(document.activeElement)) focusEl(btnClose);
  });
  return { close, goTo: (i) => go(clamp(i, 0, list.length - 1)) };
}

/** Il visualizzatore non e' una route: se il router lo richiama, torna alla galleria. */
export function mount(root, ctx) {
  root.replaceChildren();
  ctx?.navigate?.('/gallery');
}
