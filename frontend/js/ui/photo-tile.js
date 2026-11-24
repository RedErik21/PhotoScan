// Tile miniatura per griglie di foto/video. Ogni tile gestisce solo i propri eventi; l'unico listener di modulo
// e' uno scroll in fase di cattura (passivo) che serve a non animare le tile che compaiono durante lo scroll.
// Stili in css/components.css (.photo-tile*); stato visivo via data-selected, data-kind, aria-pressed (cuore), classi is-loaded / is-error.
// Le icone sono <span class="material-symbols-outlined"> (non md-icon): le tile vengono create a migliaia.
import { h } from './el.js';
import { api, fileUrl } from '../api.js';
import { formatDuration, formatDateShort } from './format.js';
import { showSnackbar } from './snackbar.js';

// Dimensione miniatura richiesta al server, in base alla dimensione della tile (px CSS) e al DPR.
// Il server accetta 96..512: il massimo e' 512.
const thumbPx = (size) => {
  const px = size * Math.min(globalThis.devicePixelRatio || 1, 3);
  return px <= 192 ? 192 : px <= 320 ? 320 : 512;
};

// ---- Animazione di ingresso ----
// Solo le tile visibili al primo passaggio si animano (fade + scala 0.98 -> 1). Sfasamento solo per le prime
// STAGGER_MAX della stessa ondata: 8 x 16 ms + --dur-short (150 ms) = 278 ms, entro i 300 ms.
const REDUCED = matchMedia('(prefers-reduced-motion: reduce)');
const STAGGER_MAX = 8;
const STAGGER_STEP_MS = 16;
const SCROLL_QUIET_MS = 150;
let lastScroll = 0;
addEventListener('scroll', () => { lastScroll = performance.now(); }, { capture: true, passive: true });

let waveFrame = 0;
let waveSlot = 0;
const io = typeof IntersectionObserver === 'function' ? new IntersectionObserver(onTilesVisible) : null;

/** Primo passaggio di una tile: se visibile e fuori da uno scroll, anima l'ingresso; altrimenti compare subito. */
function onTilesVisible(entries) {
  for (const e of entries) {
    const tile = e.target;
    io.unobserve(tile);
    tile.classList.remove('is-pending');
    if (!e.isIntersecting || performance.now() - lastScroll < SCROLL_QUIET_MS) continue;
    playEnter(tile);
  }
}

function playEnter(tile) {
  if (!waveFrame) waveFrame = requestAnimationFrame(() => { waveFrame = 0; waveSlot = 0; });
  const k = waveSlot++;
  if (k < STAGGER_MAX) tile.style.setProperty('--tile-delay', `${k * STAGGER_STEP_MS}ms`);
  tile.classList.add('is-enter');
  tile.addEventListener('animationend', () => {
    tile.classList.remove('is-enter');
    tile.style.removeProperty('--tile-delay');
  }, { once: true });
}

/** Avvia l'ingresso quando la tile e' collegata al DOM e visibile (IntersectionObserver); durante lo scroll compare subito senza flash. */
function revealTile(tile) {
  if (REDUCED.matches || !io || performance.now() - lastScroll < SCROLL_QUIET_MS) return;
  tile.classList.add('is-pending');
  io.observe(tile);
}

/** Chiamata dalla griglia quando smonta una tile: rilascia IO e annulla richieste di miniature non ancora caricate. */
export function releaseTile(tile) {
  if (!tile) return;
  io?.unobserve(tile);
  tile.classList.remove('is-pending');
  const st = tile._ft;
  if (st) {
    st.released = true;
    if (st.img && !st.img.complete) {
      st.img.removeAttribute('src');
    }
  }
}

const glyph = (name, filled = false) =>
  h('span', { class: 'material-symbols-outlined' + (filled ? ' filled' : ''), 'aria-hidden': 'true' }, name);

// "IMG_0624.HEIC" + data -> "IMG_0624, 9 ago 2022"
function altText(file) {
  const base = file.name.replace(/\.[^.]+$/, '');
  return file.date ? `${base}, ${formatDateShort(file.date)}` : base;
}

function ariaLabel(file, failed = false) {
  let s = altText(file);
  if (file.kind === 'video') s += file.duration ? `, video di ${formatDuration(file.duration)}` : ', video';
  if (failed) s += ', anteprima non disponibile';
  return s;
}

/** Crea la tile. Stato interno in tile._ft (file, opzioni, selezione, riferimenti agli elementi). */
export function createTile(file, opts = {}) {
  const o = { size: 180, selectable: false, selected: false, showMeta: false, onOpen: null, onSelectToggle: null, ...opts };
  const tile = h('div', { class: 'photo-tile', role: 'button', tabindex: '0' });
  tile.dataset.id = file.id;
  tile._ft = { file, o, selected: !!o.selected, busy: false, img: null, fb: null, badge: null, fav: null, check: null, meta: null };
  const st = tile._ft;

  mountImage(tile);
  revealTile(tile);

  tile.addEventListener('click', (e) => {
    if (e.target.closest('.photo-tile__fav, .photo-tile__check')) return;
    if (st.o.selectable && (e.ctrlKey || e.shiftKey || e.metaKey)) {
      e.preventDefault();
      toggleSelect(tile);
      return;
    }
    st.o.onOpen?.(st.file, tile);
  });

  tile.addEventListener('keydown', (e) => {
    if (e.target !== tile || e.altKey) return;
    if (e.key === 'Enter' || e.key === ' ') {
      e.preventDefault();
      st.o.onOpen?.(st.file, tile);
    } else if (!e.ctrlKey && !e.metaKey && (e.key === 'x' || e.key === 'X') && st.o.selectable) {
      e.preventDefault();
      toggleSelect(tile);
    } else if (!e.ctrlKey && !e.metaKey && (e.key === 'f' || e.key === 'F')) {
      e.preventDefault();
      toggleFavorite(tile);
    }
  });

  syncAll(tile);
  return tile;
}

/** Aggiorna una tile esistente. patch: {file?: parziale, selected?, favorite?, selectable?, showMeta?} */
export function updateTile(tile, patch = {}) {
  const st = tile._ft;
  if (!st) return tile;
  if (patch.file) {
    const prevId = st.file.id;
    st.file = { ...st.file, ...patch.file };
    if (st.file.id !== prevId) {
      tile.dataset.id = st.file.id;
      resetImage(tile);
    }
  }
  if ('selected' in patch) st.selected = !!patch.selected;
  if ('favorite' in patch) st.file = { ...st.file, favorite: !!patch.favorite };
  if ('selectable' in patch) st.o.selectable = !!patch.selectable;
  if ('showMeta' in patch) st.o.showMeta = !!patch.showMeta;
  syncAll(tile);
  return tile;
}

function mountImage(tile) {
  const st = tile._ft;
  const img = h('img', { class: 'photo-tile__img', loading: 'lazy', decoding: 'async', draggable: 'false', alt: altText(st.file) });
  img.addEventListener('load', () => { if (!st.released) img.classList.add('is-loaded'); }, { once: true });
  img.addEventListener('error', () => { if (!st.released) showFallback(tile); }, { once: true });
  img.src = fileUrl.thumb(st.file.id, thumbPx(st.o.size));
  st.img = img;
  tile.prepend(img);
}

function showFallback(tile) {
  const st = tile._ft;
  st.img?.remove();
  st.img = null;
  st.fb = h('div', { class: 'photo-tile__fallback', 'aria-hidden': 'true', title: 'Anteprima non disponibile' }, glyph('broken_image'));
  tile.prepend(st.fb);
  tile.classList.add('is-error');
  tile.setAttribute('aria-label', ariaLabel(st.file, true));
}

function resetImage(tile) {
  const st = tile._ft;
  st.img?.remove();
  st.fb?.remove();
  st.img = st.fb = null;
  tile.classList.remove('is-error');
  mountImage(tile);
}

function syncAll(tile) {
  const st = tile._ft;
  tile.dataset.kind = st.file.kind;
  tile.dataset.selected = String(st.selected);
  tile.setAttribute('aria-label', ariaLabel(st.file, !!st.fb));
  tile.setAttribute('aria-keyshortcuts', st.o.selectable ? 'F X' : 'F');
  if (st.o.selectable) tile.setAttribute('aria-pressed', String(st.selected));
  else tile.removeAttribute('aria-pressed');
  syncBadge(tile);
  syncFav(tile);
  syncCheck(tile);
  syncMeta(tile);
}

function syncBadge(tile) {
  const st = tile._ft;
  const isVideo = st.file.kind === 'video';
  if (isVideo && !st.badge) {
    st.badge = h('span', { class: 'photo-tile__badge', 'aria-hidden': 'true' }, glyph('play_arrow', true));
    tile.append(st.badge);
  } else if (!isVideo && st.badge) {
    st.badge.remove();
    st.badge = null;
  }
  if (st.badge) {
    st.badge.querySelector('.photo-tile__dur')?.remove();
    const dur = formatDuration(st.file.duration);
    if (dur) st.badge.append(h('span', { class: 'photo-tile__dur' }, dur));
  }
}

function syncFav(tile) {
  const st = tile._ft;
  if (!st.fav) {
    st.fav = h('button', { class: 'photo-tile__fav', type: 'button', tabindex: '-1' });
    st.fav.addEventListener('click', (e) => { e.stopPropagation(); toggleFavorite(tile); });
    tile.append(st.fav);
  }
  const on = !!st.file.favorite;
  st.fav.setAttribute('aria-label', on ? 'Rimuovi dai preferiti' : 'Aggiungi ai preferiti');
  st.fav.setAttribute('aria-pressed', String(on));
  st.fav.replaceChildren(glyph('favorite', on));
}

function syncCheck(tile) {
  const st = tile._ft;
  if (st.o.selectable && !st.check) {
    // components.css lo rende pointer-events:none: qui deve ricevere il click di selezione
    st.check = h('span', { class: 'photo-tile__check', 'aria-hidden': 'true', style: { pointerEvents: 'auto' } }, glyph('check'));
    st.check.addEventListener('click', (e) => { e.stopPropagation(); toggleSelect(tile); });
    tile.append(st.check);
  } else if (!st.o.selectable && st.check) {
    st.check.remove();
    st.check = null;
  }
}

function syncMeta(tile) {
  const st = tile._ft;
  if (!st.o.showMeta) { st.meta?.remove(); st.meta = null; return; }
  if (!st.meta) {
    st.meta = h('div', { class: 'photo-tile__meta', 'aria-hidden': 'true' });
    tile.append(st.meta);
  }
  const ctx = st.file.context || st.file.folder || '';
  const parts = [h('span', { class: 'photo-tile__date' }, formatDateShort(st.file.date))];
  if (ctx) parts.push(' · ', h('span', { class: 'photo-tile__ctx' }, ctx));
  st.meta.replaceChildren(...parts);
}

function toggleSelect(tile) {
  const st = tile._ft;
  if (!st.o.selectable) return;
  st.selected = !st.selected;
  syncAll(tile);
  st.o.onSelectToggle?.(st.file, st.selected, tile);
}

// Preferito con aggiornamento ottimistico; rollback + snackbar in caso di errore.
async function toggleFavorite(tile) {
  const st = tile._ft;
  if (st.busy) return;
  const prev = !!st.file.favorite;
  const next = !prev;
  st.busy = true;
  st.file = { ...st.file, favorite: next };
  syncFav(tile);
  try {
    await api.post(`/files/${st.file.id}/favorite`, { value: next });
  } catch (err) {
    st.file = { ...st.file, favorite: prev };
    syncFav(tile);
    showSnackbar(err.message || 'Impossibile aggiornare i preferiti', { error: true });
  } finally {
    st.busy = false;
  }
}
