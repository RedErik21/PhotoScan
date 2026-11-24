// Griglia di tile QUADRATE virtualizzata dentro uno scroll container (scrollEl).
//
// Layout deterministico: altezze e colonne note da `total` + `groups`, quindi il caricamento
// delle pagine non sposta mai lo scroll. Vengono montate solo le righe visibili piu' due
// schermate sopra e sotto; i nodi delle righe escluse tornano in un pool.
//
// Eventi emessi su scrollEl: 'empty' (total 0), 'error' ({error, offset|index}).
// fetchPage(offset, limit, signal): il terzo argomento e' un AbortSignal opzionale.

import { releaseTile } from './photo-tile.js';

const HEADER_H = 48;      // altezza fissa delle intestazioni di gruppo (= --vg-header-h in css/components.css)
const MAX_CONC = 4;       // richieste di pagina in volo contemporanee
const BUFFER_SCREENS = 2; // schermate di buffer sopra e sotto il viewport...
const BUFFER_MAX_PX = 1200; // ...con tetto in px: limita i nodi montati anche su finestre molto alte
const POOL_MAX = 120;     // righe DOM tenute da riusare

const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));

function normalizeGroups(groups) {
  if (!Array.isArray(groups) || !groups.length) return null;
  return groups.map((g) => ({ ...g, label: g.label ?? '', count: Math.max(0, Number(g.count) || 0), offset: Math.max(0, Number(g.offset) || 0) }));
}

function skeletonTile() {
  const d = document.createElement('div');
  d.className = 'skeleton skeleton-tile';
  d.setAttribute('aria-hidden', 'true');
  return d;
}

export class VirtualGrid {
  constructor(scrollEl, { fetchPage, renderItem, tile = 180, gap = 4, pageSize = 120, groups, renderHeader, onVisibleRange } = {}) {
    if (typeof fetchPage !== 'function' || typeof renderItem !== 'function') {
      throw new TypeError('VirtualGrid: fetchPage e renderItem sono obbligatori');
    }
    this.scrollEl = scrollEl;
    this.fetchPage = fetchPage;
    this.renderItem = renderItem;
    this.renderHeader = renderHeader || null;
    this.onVisibleRange = onVisibleRange || null;
    this.tile = tile;
    this.gap = gap;
    this.pageSize = pageSize;
    this.groups = normalizeGroups(groups);

    this.total = null;          // null finche' la prima pagina non e' arrivata
    this.items = [];            // File per indice globale (sparso)
    this.loadedPages = new Set();
    this.inflight = new Map();  // pagina -> { ctrl, pinned }
    this.queue = [];            // pagine in attesa, in ordine di priorita'
    this.gen = 0;               // incrementato a ogni reset: scarta risposte vecchie

    this.rows = [];
    this.totalHeight = 0;
    this.cols = 1;
    this.tileW = tile;
    this.lastW = -1;
    this.mounted = new Map();   // indice riga -> elemento DOM
    this.pool = [];
    this.focusIndex = 0;        // roving tabindex
    this.firstIdx = -1;
    this.lastIdx = -1;
    this.lastVa = -1;
    this.lastVb = -1;
    this.lastBa = -1;
    this.lastBb = -1;
    this.raf = 0;
    this.resizeRaf = 0;
    this.destroyed = false;

    this.layer = document.createElement('div');
    this.layer.className = 'vg-layer';
    this.layer.setAttribute('role', 'grid');
    scrollEl.append(this.layer);

    this.onScroll = () => { if (!this.raf) this.raf = requestAnimationFrame(() => this.update()); };
    this.onKeyDown = (e) => this.handleKey(e);
    this.onFocusIn = (e) => {
      const c = e.target.closest?.('[data-vg-idx]');
      if (c) { this.focusIndex = Number(c.dataset.vgIdx); this.syncTabindex(); }
    };
    scrollEl.addEventListener('scroll', this.onScroll, { passive: true });
    this.layer.addEventListener('keydown', this.onKeyDown);
    this.layer.addEventListener('focusin', this.onFocusIn);
    this.ro = new ResizeObserver(() => {
      if (!this.resizeRaf) this.resizeRaf = requestAnimationFrame(() => this.onResize());
    });
    this.ro.observe(scrollEl);
  }

  /** Carica da zero: azzera cache e legge total. Mantiene la posizione di scroll (ancora sul primo indice visibile). */
  async reload() {
    if (this.destroyed) return;
    this.reset();
    await this.startPage(0, true);
  }

  /** Porta in vista l'indice n, allineato in alto al suo gruppo/riga. */
  scrollToOffset(n) {
    if (this.destroyed || !this.total) return;
    const idx = clamp(Math.floor(n), 0, this.total - 1);
    let ri = this.findRow(idx);
    if (ri < 0) return;
    if (ri > 0 && this.rows[ri - 1].type === 'header') ri -= 1; // includi l'intestazione del gruppo
    this.scrollEl.scrollTop = this.layerOffset() + this.rows[ri].top;
    this.update();
  }

  /** Elementi caricati finora (in ordine di indice, senza buchi). */
  getItems() {
    return this.items.filter(Boolean);
  }

  /** Sostituisce i gruppi (es. cambio ordinamento) e ricalcola il layout. */
  setGroups(groups) {
    this.groups = normalizeGroups(groups);
    this.relayoutKeepingPosition();
  }

  /** Cambia la dimensione del tile (px) e ricalcola colonne e altezze. */
  setTileSize(px) {
    this.tile = px;
    this.relayoutKeepingPosition();
  }

  destroy() {
    if (this.destroyed) return;
    this.destroyed = true;
    this.gen++;
    this.abortAll();
    cancelAnimationFrame(this.raf);
    cancelAnimationFrame(this.resizeRaf);
    this.ro.disconnect();
    this.scrollEl.removeEventListener('scroll', this.onScroll);
    this.unmountAll();
    this.layer.remove();
    this.items = [];
    this.rows = [];
    this.pool = [];
  }

  // ---------- stato e caricamento pagine ----------

  reset() {
    this.gen++;
    this.abortAll();
    this.queue = [];
    this.loadedPages.clear();
    this.items = [];
    this.total = null;
    this.unmountAll();
    this.rows = [];
    this.lastVa = this.lastVb = this.lastBa = this.lastBb = -1;
    this.layer.style.height = '0px';
  }

  abortAll() {
    for (const job of this.inflight.values()) job.ctrl.abort();
    this.inflight.clear();
  }

  /** Richiede una pagina. Ritorna una Promise<boolean> (true se applicata). */
  startPage(p, pinned = false) {
    const gen = this.gen;
    const ctrl = new AbortController();
    this.inflight.set(p, { ctrl, pinned });
    const offset = p * this.pageSize;
    let req;
    try { req = Promise.resolve(this.fetchPage(offset, this.pageSize, ctrl.signal)); }
    catch (e) { req = Promise.reject(e); }
    return req
      .then((res) => {
        if (gen !== this.gen || ctrl.signal.aborted) return false;
        this.applyPage(p, offset, res);
        return true;
      }, (err) => {
        if (gen !== this.gen || ctrl.signal.aborted || err?.name === 'AbortError') return false;
        this.emit('error', { error: err, offset });
        return false;
      })
      .finally(() => {
        const cur = this.inflight.get(p);
        if (cur && cur.ctrl === ctrl) this.inflight.delete(p);
        if (gen === this.gen) this.pump();
      });
  }

  /** Pianifica le pagine in ordine di priorita'; annulla quelle in volo non piu' necessarie. */
  schedule(order) {
    const want = new Set(order);
    for (const [p, job] of this.inflight) {
      if (!job.pinned && !want.has(p)) { job.ctrl.abort(); this.inflight.delete(p); }
    }
    this.queue = order.filter((p) => !this.loadedPages.has(p) && !this.inflight.has(p));
    this.pump();
  }

  pump() {
    while (this.inflight.size < MAX_CONC && this.queue.length) {
      const p = this.queue.shift();
      if (!this.loadedPages.has(p) && !this.inflight.has(p)) this.startPage(p);
    }
  }

  applyPage(p, offset, res) {
    const total = Math.max(0, Number(res && res.total) || 0);
    const list = Array.isArray(res && res.items) ? res.items : [];
    if (total !== this.total) {
      this.total = total;
      this.items.length = total;
      for (const q of [...this.loadedPages]) if (q * this.pageSize >= total) this.loadedPages.delete(q);
      this.unmountAll();
      this.relayout(this.firstIdx);
      if (total === 0) this.emit('empty');
    }
    const n = Math.min(list.length, Math.max(0, total - offset));
    for (let i = 0; i < n; i++) this.items[offset + i] = list[i];
    this.loadedPages.add(p);
    this.refreshRange(offset, offset + n);
    this.update();
  }

  // ---------- layout ----------

  /** Offset verticale del layer nel contenuto dello scroll container (indipendente dallo scroll). */
  layerOffset() {
    const l = this.layer.getBoundingClientRect();
    const s = this.scrollEl.getBoundingClientRect();
    return l.top - s.top - this.scrollEl.clientTop + this.scrollEl.scrollTop;
  }

  /** Ricalcola colonne e righe. `anchor` = indice da mantenere in cima dopo il ricalcolo. */
  relayout(anchor = -1) {
    const total = this.total ?? 0;
    const W = this.layer.clientWidth;
    this.lastW = W;
    this.cols = Math.max(1, Math.floor((W + this.gap) / (this.tile + this.gap)));
    this.tileW = Math.max(1, Math.floor((W - this.gap * (this.cols - 1)) / this.cols));
    const pitch = this.tileW + this.gap;
    const list = this.groups
      ? this.groups.filter((g) => g.count > 0 && g.offset < total)
      : [{ count: total, offset: 0 }];

    const rows = [];
    let y = 0;
    for (const g of list) {
      if (this.groups) { rows.push({ type: 'header', top: y, height: HEADER_H, group: g }); y += HEADER_H; }
      const end = Math.min(g.offset + g.count, total);
      for (let s = g.offset; s < end; s += this.cols) {
        rows.push({ type: 'items', top: y, height: this.tileW, start: s, end: Math.min(s + this.cols, end) });
        y += pitch;
      }
    }
    this.rows = rows;
    this.totalHeight = y;
    this.layer.style.height = `${y}px`;
    this.layer.style.setProperty('--vg-tile', `${this.tileW}px`);

    if (anchor >= 0 && total > 0) {
      const ri = this.findRow(clamp(anchor, 0, total - 1));
      if (ri >= 0) this.scrollEl.scrollTop = this.layerOffset() + rows[ri].top;
    }
  }

  relayoutKeepingPosition() {
    if (this.total === null || this.destroyed) return;
    this.unmountAll();
    this.lastVa = this.lastVb = this.lastBa = this.lastBb = -1;
    this.relayout(this.firstIdx);
    this.update();
  }

  onResize() {
    this.resizeRaf = 0;
    if (this.destroyed || this.total === null) return;
    if (this.layer.clientWidth === this.lastW) { this.update(); return; }
    this.relayoutKeepingPosition();
  }

  /** Indice (a righe) della prima riga che interseca [y0, y1). */
  rowRange(y0, y1) {
    const rows = this.rows;
    let lo = 0, hi = rows.length;
    while (lo < hi) {
      const mid = (lo + hi) >> 1;
      const r = rows[mid];
      if (r.top + r.height <= y0) lo = mid + 1; else hi = mid;
    }
    let b = lo;
    while (b < rows.length && rows[b].top < y1) b++;
    return [lo, b];
  }

  /** Primo e ultimo indice di elementi nelle righe [a, b). null se non ci sono elementi. */
  itemSpan(a, b) {
    let first = Infinity, last = -1;
    for (let r = a; r < b; r++) {
      const row = this.rows[r];
      if (row.type !== 'items') continue;
      if (row.start < first) first = row.start;
      if (row.end - 1 > last) last = row.end - 1;
    }
    return last < 0 ? null : { first, last };
  }

  findRow(idx) {
    for (let r = 0; r < this.rows.length; r++) {
      const row = this.rows[r];
      if (row.type === 'items' && idx >= row.start && idx < row.end) return r;
    }
    return -1;
  }

  // ---------- rendering delle righe visibili ----------

  update() {
    this.raf = 0;
    if (this.destroyed || this.total === null || !this.rows.length) return;
    const off = this.layerOffset();
    const vh = this.scrollEl.clientHeight;
    const vTop = this.scrollEl.scrollTop - off;
    const vBot = vTop + vh;
    const [va, vb] = this.rowRange(vTop, vBot);
    const buffer = Math.min(BUFFER_SCREENS * vh, BUFFER_MAX_PX);
    const [ba, bb] = this.rowRange(vTop - buffer, vBot + buffer);

    if (va === this.lastVa && vb === this.lastVb && ba === this.lastBa && bb === this.lastBb) return;
    this.lastVa = va;
    this.lastVb = vb;
    this.lastBa = ba;
    this.lastBb = bb;

    const vis = this.itemSpan(va, vb);
    if (vis && (vis.first !== this.firstIdx || vis.last !== this.lastIdx)) {
      this.firstIdx = vis.first;
      this.lastIdx = vis.last;
      this.onVisibleRange?.(vis.first, vis.last);
    }

    // Pagine necessarie: prima quelle visibili, poi il buffer per distanza dal viewport.
    const buf = this.itemSpan(ba, bb);
    if (buf) {
      const pg = (i) => Math.floor(i / this.pageSize);
      const order = [];
      const seen = new Set();
      if (vis) for (let p = pg(vis.first); p <= pg(vis.last); p++) { order.push(p); seen.add(p); }
      const center = vis ? pg(vis.first) : pg(buf.first);
      const rest = [];
      for (let p = pg(buf.first); p <= pg(buf.last); p++) if (!seen.has(p)) rest.push(p);
      rest.sort((a, b) => Math.abs(a - center) - Math.abs(b - center));
      this.schedule(order.concat(rest));
    } else {
      this.schedule([]);
    }

    for (const ri of [...this.mounted.keys()]) if (ri < ba || ri >= bb) this.unmountRow(ri);
    let mountFrag = null;
    for (let ri = ba; ri < bb; ri++) {
      if (!this.mounted.has(ri)) {
        if (!mountFrag) mountFrag = document.createDocumentFragment();
        mountFrag.append(this.buildRow(ri));
      }
    }
    if (mountFrag) this.layer.append(mountFrag);
  }

  buildRow(ri) {
    const row = this.rows[ri];
    const el = this.pool.pop() || document.createElement('div');
    el.className = row.type === 'header' ? 'vg-row vg-header' : 'vg-row';
    el.setAttribute('role', row.type === 'header' ? 'presentation' : 'row');
    el.style.transform = `translate3d(0, ${row.top}px, 0)`;
    el.style.height = `${row.height}px`;
    el.style.gridTemplateColumns = row.type === 'items' ? `repeat(${this.cols}, ${this.tileW}px)` : '';
    el.style.columnGap = row.type === 'items' ? `${this.gap}px` : '';
    if (row.type === 'header') el.replaceChildren(this.buildHeader(row.group));
    else {
      const frag = document.createDocumentFragment();
      for (let i = row.start; i < row.end; i++) frag.append(this.buildCell(i));
      el.replaceChildren(frag);
    }
    this.mounted.set(ri, el);
    return el;
  }

  unmountRow(ri) {
    const el = this.mounted.get(ri);
    if (!el) return;
    this.mounted.delete(ri);
    for (const t of el.querySelectorAll('.photo-tile')) releaseTile(t);
    el.replaceChildren();
    el.remove();
    if (this.pool.length < POOL_MAX) this.pool.push(el);
  }

  unmountAll() {
    for (const ri of [...this.mounted.keys()]) this.unmountRow(ri);
    this.lastVa = this.lastVb = this.lastBa = this.lastBb = -1;
  }

  buildCell(i) {
    const cell = document.createElement('div');
    cell.className = 'vg-cell';
    cell.setAttribute('role', 'gridcell');
    cell.dataset.vgIdx = i;
    cell.tabIndex = i === this.focusIndex ? 0 : -1;
    cell.style.height = `${this.tileW}px`;
    this.fillCell(cell, i);
    return cell;
  }

  /** Riempie la cella: tile reale se l'elemento e' caricato, altrimenti segnaposto. */
  fillCell(cell, i) {
    const file = this.items[i];
    let node = null;
    if (file) {
      try { node = this.renderItem(file, i); }
      catch (error) { this.emit('error', { error, index: i }); }
    }
    cell.dataset.pending = node instanceof Node ? '0' : '1';
    cell.replaceChildren(node instanceof Node ? node : skeletonTile());
    // Il tab stop e' la cella (roving tabindex): la tile figlia non deve essere un secondo tab stop.
    if (node instanceof Node && node.tabIndex === 0) node.tabIndex = -1;
  }

  /** Aggiorna i campi di un elemento (per id) e ridisegna la sua cella se montata. */
  patchItem(id, patch) {
    if (this.destroyed) return;
    const i = this.items.findIndex((f) => f && f.id === id);
    if (i < 0) return;
    const cur = this.items[i];
    if (!Object.keys(patch).some((k) => cur[k] !== patch[k])) return;
    this.items[i] = { ...cur, ...patch };
    const cell = this.layer.querySelector(`[data-vg-idx="${i}"]`);
    if (!cell) return;
    const active = document.activeElement;
    const onTile = !!active && active !== cell && cell.contains(active);
    this.fillCell(cell, i);
    if (onTile) cell.firstElementChild?.focus({ preventScroll: true });
  }

  buildHeader(group) {
    let node = null;
    if (this.renderHeader) {
      try { node = this.renderHeader(group); }
      catch (error) { this.emit('error', { error, group }); }
    }
    if (node instanceof Node) {
      const slot = document.createElement('div');
      slot.className = 'vg-header-slot';
      slot.append(node);
      return slot;
    }
    const h = document.createElement('div');
    h.className = 'tile-group-header';
    h.setAttribute('role', 'heading');
    h.setAttribute('aria-level', '2');
    const span = document.createElement('span');
    span.className = 'md-typescale-title-medium';
    span.textContent = group.label;
    h.append(span);
    return h;
  }

  /** Sostituisce i segnaposto delle righe montate la cui pagina e' arrivata (il nodo cella resta, il focus anche). */
  refreshRange(a, b) {
    for (const [ri, el] of this.mounted) {
      const row = this.rows[ri];
      if (row.type !== 'items' || row.start >= b || row.end <= a) continue;
      for (const cell of el.children) {
        if (cell.dataset.pending === '1') {
          const i = Number(cell.dataset.vgIdx);
          if (this.items[i]) this.fillCell(cell, i);
        }
      }
    }
  }

  syncTabindex() {
    for (const c of this.layer.querySelectorAll('[data-vg-idx]')) {
      c.tabIndex = Number(c.dataset.vgIdx) === this.focusIndex ? 0 : -1;
    }
  }

  // ---------- tastiera (roving tabindex) ----------

  /** Indice dell'elemento alla stessa colonna, `n` righe visuali piu' in basso (n>0) o in alto (n<0); salta le intestazioni. */
  indexRowShift(cur, n) {
    const ri = this.findRow(cur);
    if (ri < 0) return cur;
    const col = cur - this.rows[ri].start;
    const dir = Math.sign(n);
    let r = ri;
    for (let left = Math.abs(n); left > 0; left--) {
      let k = r + dir;
      while (k >= 0 && k < this.rows.length && this.rows[k].type !== 'items') k += dir;
      if (k < 0 || k >= this.rows.length) break;
      r = k;
    }
    const row = this.rows[r];
    return Math.min(row.start + col, row.end - 1);
  }

  handleKey(e) {
    // Enter/Spazio gia' gestiti dalla tile (preventDefault): non riaprire.
    if (e.defaultPrevented || !this.total || e.target === this.layer) return;
    const cell = e.target.closest?.('[data-vg-idx]');
    // Focus sulla cella o sulla tile figlia diretta; i controlli annidati (cuore) non li gestiamo.
    if (!cell || (e.target !== cell && e.target.parentElement !== cell)) return;
    const cur = Number(cell.dataset.vgIdx);
    const pageRows = Math.max(1, Math.floor(this.scrollEl.clientHeight / (this.tileW + this.gap)));
    let next;
    switch (e.key) {
      case 'ArrowRight': next = cur + 1; break;
      case 'ArrowLeft': next = cur - 1; break;
      case 'ArrowDown': next = this.indexRowShift(cur, 1); break;
      case 'ArrowUp': next = this.indexRowShift(cur, -1); break;
      case 'PageDown': next = this.indexRowShift(cur, pageRows); break;
      case 'PageUp': next = this.indexRowShift(cur, -pageRows); break;
      case 'Home': next = 0; break;
      case 'End': next = this.total - 1; break;
      case 'Enter':
      case ' ':
        e.preventDefault();
        cell.firstElementChild?.click();
        return;
      default: return;
    }
    e.preventDefault();
    next = clamp(next, 0, this.total - 1);
    if (next === cur) return;
    this.focusIndex = next;
    this.ensureVisible(next);
    const target = this.layer.querySelector(`[data-vg-idx="${next}"]`);
    this.syncTabindex();
    target?.focus({ preventScroll: true });
  }

  ensureVisible(idx) {
    const ri = this.findRow(idx);
    if (ri < 0) return;
    const off = this.layerOffset();
    const row = this.rows[ri];
    const top = off + row.top;
    const bot = top + row.height;
    const sTop = this.scrollEl.scrollTop;
    const vh = this.scrollEl.clientHeight;
    if (top < sTop) this.scrollEl.scrollTop = top;
    else if (bot > sTop + vh) this.scrollEl.scrollTop = bot - vh;
    this.update();
  }

  emit(name, detail = {}) {
    this.scrollEl.dispatchEvent(new CustomEvent(name, { detail }));
  }
}
