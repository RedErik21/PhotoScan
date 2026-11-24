// Barra di scorrimento temporale (stile Google Foto) sul bordo destro del contenitore `host`.
//   createScrubber(host, {buckets:[{key,count,offset}], onSeek(offset), getScrollRatio?})
//     -> {update(offsetVisibile), setBuckets(buckets), destroy()}
// `host` deve essere non scorrevole (il rail e' assoluto, a tutta altezza, sul bordo destro).
// Se getScrollRatio e' fornito, la posizione del thumb segue il rapporto di scroll (0..1); altrimenti offset/totale.
import { formatMonth, mesiNomi } from './format.js';

const PAD = 20;               // meta' del thumb: la traccia va da PAD a altezza-PAD
const YEAR_GAP = 28;          // distanza minima (px) tra due etichette anno per evitare sovrapposizioni
const TICK_GAP = 4;           // distanza minima (px) tra due tacche mese
const HIDE_MS = 1500;         // auto-hide su schermi compatti
const COMPACT_QUERY = '(max-width: 599.98px)';
const KEY_RE = /^(\d{4})-(\d{2})(?:-(\d{2}))?$/;
const SPECIAL = { unknown: 'Senza data', uncertain: 'Data incerta' };

const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));

function el(tag, attrs = {}) {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === 'class') n.className = v;
    else n.setAttribute(k, v);
  }
  return n;
}

/** Normalizza i bucket: etichetta italiana, gruppi per anno, totale. */
function prepare(buckets) {
  const list = (buckets || []).map((b) => {
    const m = KEY_RE.exec(b.key || '');
    const year = m ? +m[1] : null;
    const month = m ? +m[2] : null;
    const day = m && m[3] ? +m[3] : null;
    const special = SPECIAL[b.key] || null; // 'Senza data' / 'Data incerta': bucket senza anno
    const label = special || (!m ? 'Senza data' : day ? `${day} ${mesiNomi[month - 1]} ${year}` : formatMonth(year, month));
    return { offset: b.offset || 0, count: b.count || 0, year, label, special, yearStart: false };
  }).sort((a, b) => a.offset - b.offset);

  const groups = [];
  list.forEach((b, i) => {
    const prev = list[i - 1];
    if (!prev || prev.year !== b.year || prev.special !== b.special) {
      b.yearStart = true;
      groups.push({ offset: b.offset, label: b.special || String(b.year) });
    }
  });
  const total = list.reduce((max, b) => Math.max(max, b.offset + b.count), 0);
  return { list, groups, total };
}

export function createScrubber(host, { buckets = [], onSeek = () => {}, getScrollRatio = null } = {}) {
  if (getComputedStyle(host).position === 'static') host.style.position = 'relative';

  const rail = el('div', { class: 'scrubber' });
  const ticksEl = el('div', { class: 'scrubber__ticks', 'aria-hidden': 'true' });
  const labelsEl = el('div', { class: 'scrubber__labels', 'aria-hidden': 'true' });
  const tooltip = el('div', { class: 'scrubber__tooltip', role: 'presentation', 'aria-hidden': 'true' });
  const thumb = el('div', { class: 'scrubber__thumb', role: 'slider', tabindex: '0', 'aria-label': 'Scorri per data', 'aria-orientation': 'vertical' });
  rail.append(ticksEl, labelsEl, tooltip, thumb);
  host.append(rail);

  const compactMql = matchMedia(COMPACT_QUERY);
  let data = prepare(buckets);
  let current = 0;          // offset visibile / selezionato
  let dragging = false;
  let kbActive = false;     // thumb con focus da tastiera: tooltip visibile
  let grab = 0;             // distanza pointer-centro thumb al momento del pressing
  let railH = 0;
  let railTop = 0;
  let trackH = 0;
  let thumbY = 0;
  let lastLabel = null;
  let lastActiveYear = null;
  let compact = false;
  let hideTimer = 0;
  let rafId = 0;
  let pending = null;
  let destroyed = false;

  const maxOffset = () => Math.max(0, data.total - 1);
  const clampOff = (o) => clamp(Math.floor(Number(o) || 0), 0, maxOffset());
  const yFromRatio = (r) => PAD + clamp(r, 0, 1) * trackH;

  function labelAt(off) {
    const list = data.list;
    if (!list.length) return '';
    let lo = 0, hi = list.length - 1, ans = list[0];
    while (lo <= hi) {
      const mid = (lo + hi) >> 1;
      if (list[mid].offset <= off) { ans = list[mid]; lo = mid + 1; } else hi = mid - 1;
    }
    return ans.label;
  }

  let labelNodes = [];      // { label, el } per evidenziare l'anno attivo

  function highlightActiveYear() {
    if (!labelNodes.length) return;
    let activeLabel = data.groups[0]?.label || '';
    for (const g of data.groups) {
      if (g.offset <= current) activeLabel = g.label; else break;
    }
    if (activeLabel === lastActiveYear) return;
    lastActiveYear = activeLabel;
    for (const node of labelNodes) {
      node.el.classList.toggle('is-active', node.label === activeLabel);
    }
  }

  function syncAria() {
    const label = labelAt(current);
    thumb.setAttribute('aria-valuenow', String(current));
    if (label !== lastLabel) {
      lastLabel = label;
      thumb.setAttribute('aria-valuemin', '0');
      thumb.setAttribute('aria-valuemax', String(maxOffset()));
      thumb.setAttribute('aria-valuetext', label);
      tooltip.textContent = label;
    }
    highlightActiveYear();
  }

  function setThumbY(y) {
    thumbY = y;
    const t = `translate3d(0, ${y}px, 0) translateY(-50%)`;
    thumb.style.transform = t;
    tooltip.style.transform = t;
  }

  function placeThumb() {
    if (dragging) return;   // durante il trascinamento decide il puntatore
    const r = getScrollRatio ? Number(getScrollRatio()) : (data.total ? current / data.total : 0);
    setThumbY(yFromRatio(Number.isFinite(r) ? r : 0));
  }

  function setTipActive(on) { tooltip.classList.toggle('is-active', on); }

  function reveal() {
    if (!compact) return;
    rail.classList.add('is-visible');
    clearTimeout(hideTimer);
    if (!dragging && !kbActive) hideTimer = setTimeout(() => rail.classList.remove('is-visible'), HIDE_MS);
  }

  /** Ricostruisce tacche ed etichette in base all'altezza attuale del rail. */
  function layout() {
    const rect = rail.getBoundingClientRect();
    railH = rail.clientHeight;
    railTop = rect.top;
    trackH = Math.max(0, railH - 2 * PAD);
    ticksEl.replaceChildren();
    labelsEl.replaceChildren();
    labelNodes = [];
    lastActiveYear = null;
    lastLabel = null;
    rail.classList.toggle('is-empty', !data.total);
    if (!data.total || !trackH) { placeThumb(); return; }

    const yOf = (off) => yFromRatio(off / data.total);
    let lastLabelY = -Infinity;
    for (const g of data.groups) {
      const y = yOf(g.offset);
      ticksEl.append(tick(y, 'year'));
      if (y - lastLabelY >= YEAR_GAP) {
        const l = el('span', { class: 'scrubber__label' });
        l.textContent = g.label;
        l.style.top = `${y}px`;
        labelsEl.append(l);
        labelNodes.push({ label: g.label, el: l });
        lastLabelY = y;
      }
    }
    highlightActiveYear();
    // Tacche mese: solo dove c'e' spazio; l'inizio anno e' gia' coperto dalla tacca anno.
    let lastTickY = -Infinity;
    for (const b of data.list) {
      if (b.yearStart) continue;
      const y = yOf(b.offset);
      if (y - lastTickY < TICK_GAP) continue;
      ticksEl.append(tick(y, 'month'));
      lastTickY = y;
    }
    placeThumb();
  }

  function tick(y, kind) {
    const t = el('div', { class: `scrubber__tick scrubber__tick--${kind}` });
    t.style.top = `${y}px`;
    return t;
  }

  // Seek con throttle a requestAnimationFrame: si emette sempre l'ultimo valore.
  function flushSeek() {
    rafId = 0;
    if (pending === null) return;
    const v = pending;
    pending = null;
    if (!destroyed) onSeek(v);
  }
  function requestSeek(off) {
    pending = off;
    if (!rafId) rafId = requestAnimationFrame(flushSeek);
  }
  function endSeek() {
    if (rafId) cancelAnimationFrame(rafId);
    flushSeek();
  }

  function stepBucket(dir) {
    const list = data.list;
    if (dir < 0) {
      for (let i = list.length - 1; i >= 0; i--) if (list[i].offset < current) return list[i].offset;
      return 0;
    }
    for (const b of list) if (b.offset > current) return b.offset;
    return maxOffset();
  }

  function stepGroup(dir) {
    const g = data.groups;
    if (dir < 0) {
      for (let i = g.length - 1; i >= 0; i--) if (g[i].offset < current) return g[i].offset;
      return 0;
    }
    for (const x of g) if (x.offset > current) return x.offset;
    return maxOffset();
  }

  function onKeyDown(e) {
    if (!data.total) return;
    let target;
    switch (e.key) {
      case 'ArrowUp': case 'ArrowLeft': target = stepBucket(-1); break;
      case 'ArrowDown': case 'ArrowRight': target = stepBucket(1); break;
      case 'PageUp': target = stepGroup(-1); break;
      case 'PageDown': target = stepGroup(1); break;
      case 'Home': target = 0; break;
      case 'End': target = maxOffset(); break;
      default: return;
    }
    e.preventDefault();
    kbActive = true;
    setTipActive(true);
    current = clampOff(target);
    syncAria();
    placeThumb();
    requestSeek(current);
    reveal();
  }

  function onThumbBlur() {
    kbActive = false;
    setTipActive(dragging);
    reveal();
  }

  /** Porta il thumb alla coordinata del puntatore (clientY) e richiede il seek. */
  function moveTo(clientY) {
    const y = clamp(clientY - railTop - grab, PAD, PAD + trackH);
    const r = trackH ? (y - PAD) / trackH : 0;
    current = clampOff(Math.floor(r * data.total));
    setThumbY(y);
    syncAria();
    requestSeek(current);
  }

  function onPointerDown(e) {
    if (!data.total || (e.pointerType === 'mouse' && e.button !== 0)) return;
    e.preventDefault();
    dragging = true;
    rail.classList.add('is-dragging');
    rail.setPointerCapture(e.pointerId);
    railTop = rail.getBoundingClientRect().top;
    grab = 0;
    if (e.target === thumb) {
      grab = e.clientY - (railTop + thumbY);
    }
    thumb.focus({ preventScroll: true });
    setTipActive(true);
    reveal();
    moveTo(e.clientY);
  }

  function onPointerMove(e) {
    if (dragging) moveTo(e.clientY);
  }

  function endDrag() {
    if (!dragging) return;
    dragging = false;
    rail.classList.remove('is-dragging');
    endSeek();
    setTipActive(kbActive);
    placeThumb();
    reveal();
  }

  rail.addEventListener('pointerdown', onPointerDown);
  rail.addEventListener('pointermove', onPointerMove);
  rail.addEventListener('pointerup', endDrag);
  rail.addEventListener('pointercancel', endDrag);
  rail.addEventListener('lostpointercapture', endDrag);
  thumb.addEventListener('keydown', onKeyDown);
  thumb.addEventListener('blur', onThumbBlur);

  const onCompactChange = () => {
    compact = compactMql.matches;
    rail.classList.toggle('is-compact', compact);
    rail.classList.remove('is-visible');
    reveal(); // su compatto mostra la barra per un attimo, cosi' l'utente la scopre
  };
  compactMql.addEventListener('change', onCompactChange);
  onCompactChange();

  const ro = typeof ResizeObserver !== 'undefined' ? new ResizeObserver(() => layout()) : null;
  if (ro) ro.observe(rail);
  layout();
  syncAria();

  function update(offset) {
    if (destroyed) return;
    current = clampOff(offset);
    syncAria();
    placeThumb();
    reveal();
  }

  function setBuckets(newBuckets) {
    if (destroyed) return;
    data = prepare(newBuckets);
    current = clampOff(current);
    layout();
    syncAria();
  }

  function destroy() {
    if (destroyed) return;
    destroyed = true;
    clearTimeout(hideTimer);
    if (rafId) cancelAnimationFrame(rafId);
    rafId = 0;
    pending = null;
    if (ro) ro.disconnect();
    compactMql.removeEventListener('change', onCompactChange);
    rail.remove();
  }

  return { update, setBuckets, destroy };
}
