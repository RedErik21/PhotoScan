// Luoghi: foto con posizione GPS raggruppate per zona, mini-mappa SVG offline e persone (se il riconoscimento volti e' attivo).
// Dati: GET /places, GET /places/files, GET /people, GET /settings. Nessuna mappa a tile: l'app funziona senza rete.
import { api, fileUrl } from '../api.js';
import { h, icon } from '../ui/el.js';
import { filterChips } from '../ui/chips.js';
import { emptyState, errorState } from '../ui/empty-state.js';
import { createTile } from '../ui/photo-tile.js';
import { skeletonCard, skeletonGrid } from '../ui/skeleton.js';
import { showSnackbar } from '../ui/snackbar.js';
import { plural } from '../ui/format.js';

const PRECISION = [
  { id: 1, label: 'Città', hint: 'Zone ampie: una per città o paese' },
  { id: 2, label: 'Quartiere', hint: 'Zone piccole: più quartieri distinti' },
];
const CARDS_PAGE = 60;      // card dei luoghi per blocco
const FILES_PAGE = 120;     // foto per pagina nel dettaglio
const LOW_GPS = 10;         // sotto questa soglia spieghiamo che ci sono poche foto con posizione
const MAP_W = 600;          // larghezza del viewBox della mappa
const SVG_NS = 'http://www.w3.org/2000/svg';
const NAME_LABELS = 5;      // quanti cerchi (i più grandi) portano il nome scritto sulla mappa
const LABEL_PX = 13;        // dimensione del nome sulla mappa, in pixel schermo (resta leggibile con lo zoom)
const MIN_HIT_PX = 24;      // raggio minimo, in pixel, dell'area toccabile di ogni cerchio

/** Carica il foglio di stile della view una sola volta (css/view-places.css). */
function loadCss() {
  if (document.querySelector('link[data-view-css="places"]')) return;
  const link = document.createElement('link');
  link.rel = 'stylesheet';
  link.href = new URL('../../css/view-places.css', import.meta.url).href;
  link.dataset.viewCss = 'places';
  document.head.append(link);
}

/** Icona decorativa (nascosta agli screen reader). */
function deco(name, extraClass = '') {
  const ic = icon(name);
  ic.setAttribute('aria-hidden', 'true');
  if (extraClass) ic.classList.add(extraClass);
  return ic;
}

/** Elemento SVG con attributi e figli. */
function svg(tag, attrs = {}, ...children) {
  const el = document.createElementNS(SVG_NS, tag);
  for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, String(v));
  el.append(...children);
  return el;
}

/** Miniatura con segnaposto se il server non riesce a generarla. */
function thumbImg(id, size, alt) {
  const img = h('img', { src: fileUrl.thumb(id, size), alt, loading: 'lazy', decoding: 'async' });
  img.addEventListener('error', () => {
    img.replaceWith(h('div', { class: 'places-fallback', 'aria-hidden': 'true' }, icon('image_not_supported')));
  }, { once: true });
  return img;
}

/**
 * Mini-mappa SVG: proiezione equirettangolare con correzione del coseno alla latitudine media,
 * zoom automatico sul bounding box dei dati. Cerchi proporzionali al conteggio, cliccabili da tastiera.
 */
// Confini dei paesi (Natural Earth 110m, dominio pubblico) incluso nel progetto: nessuna rete, nessun dato inviato.
const WORLD_URL = new URL('../../data/ne_110m_admin_0_countries.geojson', import.meta.url).href;
let worldCache = null;
let worldData = null;   // GeoJSON già caricato (per disegnare subito alle visite successive)
function loadWorld() {
  if (!worldCache) {
    worldCache = fetch(WORLD_URL)
      .then((r) => (r.ok ? r.json() : null))
      .then((json) => { worldData = json; return json; })
      .catch(() => null);
  }
  return worldCache;
}

// Città e luoghi noti (data/cities.json: [nome, lat, lon]) per dare un nome leggibile ai cluster. Offline.
const CITIES_URL = new URL('../../data/cities.json', import.meta.url).href;
const NEAR_KM = 15;         // entro questa distanza: "Nome"
const NEARBY_KM = 50;       // entro questa distanza: "Vicino a Nome"; oltre resta l'etichetta del server
let citiesCache = null;
let cities = null;          // elenco caricato; null se non disponibile (si usano le etichette del server)
function loadCities() {
  if (!citiesCache) {
    citiesCache = fetch(CITIES_URL)
      .then((r) => (r.ok ? r.json() : null))
      .then((json) => { cities = Array.isArray(json) ? json : null; return cities; })
      .catch(() => null);
  }
  return citiesCache;
}

/** Distanza in km tra due coordinate (formula di haversine). */
function distanceKm(lat1, lon1, lat2, lon2) {
  const rad = Math.PI / 180;
  const dLat = (lat2 - lat1) * rad, dLon = (lon2 - lon1) * rad;
  const a = Math.sin(dLat / 2) ** 2 + Math.cos(lat1 * rad) * Math.cos(lat2 * rad) * Math.sin(dLon / 2) ** 2;
  return 2 * 6371 * Math.asin(Math.min(1, Math.sqrt(a)));
}

const DIRS = ['nord', 'nord-est', 'est', 'sud-est', 'sud', 'sud-ovest', 'ovest', 'nord-ovest'];

/** Città più vicina al cluster: { city, km, dir } (dir = direzione dalla città al cluster), oppure null. */
function nearestCity(c) {
  if (!cities?.length) return null;
  let best = null, bestKm = Infinity;
  for (const entry of cities) {
    const km = distanceKm(c.lat, c.lon, entry[1], entry[2]);
    if (km < bestKm) { bestKm = km; best = entry; }
  }
  const [city, lat, lon] = best;
  const north = (c.lat - lat) * 111;
  const east = (c.lon - lon) * 111 * Math.cos((lat * Math.PI) / 180);
  const angle = ((Math.atan2(east, north) * 180) / Math.PI + 360) % 360;
  return { city, km: bestKm, dir: DIRS[Math.round(angle / 45) % 8] };
}

/** Nome leggibile del cluster: città più vicina, "Vicino a …", oppure l'etichetta con le coordinate. */
function placeOf(c) {
  const near = nearestCity(c);
  if (!near) return { name: c.label, city: null, near: null };
  if (near.km <= NEAR_KM) return { name: near.city, city: near.city, near };
  if (near.km <= NEARBY_KM) return { name: `Vicino a ${near.city}`, city: near.city, near };
  return { name: c.label, city: null, near: null };
}

/**
 * Nomi unici: se più luoghi risultano "Roma", ognuno prende il suo complemento
 * ("Roma · centro" oppure "Roma · nord-est 8 km"). Le coordinate restano solo se il nome è ancora uguale.
 */
function labelClusters(list) {
  const tally = (key) => {
    const m = new Map();
    for (const c of list) m.set(c[key], (m.get(c[key]) || 0) + 1);
    return m;
  };
  const names = tally('name');
  for (const c of list) {
    if (names.get(c.name) > 1 && c.city && c.near) {
      c.title = c.near.km <= 3 ? `${c.city} · centro` : `${c.city} · ${c.near.dir} ${Math.round(c.near.km)} km`;
    }
  }
  const titles = tally('title');
  for (const c of list) {
    c.name = c.title;
    c.showCoords = titles.get(c.title) > 1;
  }
  return list;
}

/** Percorso SVG di una geometria GeoJSON (Polygon/MultiPolygon) proiettata con x/y. */
function geometryPath(geom, x, y) {
  const polys = geom.type === 'Polygon' ? [geom.coordinates] : geom.type === 'MultiPolygon' ? geom.coordinates : [];
  let d = '';
  for (const poly of polys) {
    for (const ring of poly) {
      if (!ring.length) continue;
      d += 'M' + ring.map(([lon, lat]) => `${x(lon).toFixed(1)} ${y(lat).toFixed(1)}`).join('L') + 'Z';
    }
  }
  return d;
}

/** Vero se il riquadro (bounding box) della geometria tocca l'area [w,e]x[s,n] in gradi. */
function geomIntersects(geom, w, e, s, n) {
  const polys = geom.type === 'Polygon' ? [geom.coordinates] : geom.type === 'MultiPolygon' ? geom.coordinates : [];
  let x0 = Infinity, x1 = -Infinity, y0 = Infinity, y1 = -Infinity;
  for (const poly of polys) for (const ring of poly) for (const [lon, lat] of ring) {
    if (lon < x0) x0 = lon;
    if (lon > x1) x1 = lon;
    if (lat < y0) y0 = lat;
    if (lat > y1) y1 = lat;
  }
  return x1 >= w && x0 <= e && y1 >= s && y0 <= n;
}

function buildMap(clusters, onSelect, world = null) {
  const lats = clusters.map((c) => c.lat);
  const lons = clusters.map((c) => c.lon);
  const minLat = Math.min(...lats), maxLat = Math.max(...lats);
  const minLon = Math.min(...lons), maxLon = Math.max(...lons);
  const midLat = (minLat + maxLat) / 2;
  const kx = Math.cos((midLat * Math.PI) / 180);
  // Area minima di 0,5° per lato e margine del 12% per non tagliare i cerchi ai bordi.
  const spanLat = Math.max(maxLat - minLat, 0.5) * 1.24;
  const spanLon = Math.max(maxLon - minLon, 0.5) * 1.24;
  const north = midLat + spanLat / 2;
  const south = north - spanLat;
  const west = (minLon + maxLon) / 2 - spanLon / 2;
  const east = west + spanLon;
  const s = MAP_W / (spanLon * kx); // px per grado, uguale sui due assi
  const H = spanLat * s;
  const x = (lon) => (lon - west) * kx * s;
  const y = (lat) => (north - lat) * s;

  // Sfondo geografico: solo i paesi che toccano l'area visibile, sotto griglia e punti.
  const land = [];
  if (world?.features) {
    for (const f of world.features) {
      if (!f.geometry || !geomIntersects(f.geometry, west, east, south, north)) continue;
      const d = geometryPath(f.geometry, x, y);
      if (d) land.push(svg('path', { d, class: 'places-map__land' }));
    }
  }

  // Griglia di meridiani/paralleli (solo linee, niente numeri: i nomi dei luoghi dicono dove siamo).
  const deg = Math.max(spanLat, spanLon);
  const step = [0.5, 1, 2, 5, 10].find((st) => deg / st <= 6) ?? 10;
  const grid = [];
  for (let lat = Math.ceil(south / step) * step; lat <= north; lat += step) {
    const py = y(lat);
    grid.push(svg('line', { x1: 0, x2: MAP_W, y1: py, y2: py, class: 'places-map__line' }));
  }
  for (let lon = Math.ceil(west / step) * step; lon <= east; lon += step) {
    const px = x(lon);
    grid.push(svg('line', { x1: px, x2: px, y1: 0, y2: H, class: 'places-map__line' }));
  }

  // I cluster sono gia' ordinati per conteggio decrescente: i cerchi piccoli restano sopra.
  // I cluster sono gia' ordinati per conteggio decrescente: i cerchi piccoli restano sopra.
  // Il tasto da tastiera e' la lista delle card: i cerchi sono nascosti ai lettori di schermo e non entrano nella Tab.
  const maxCount = Math.max(...clusters.map((c) => c.count));
  const dotsG = svg('g', { class: 'places-map__dots', 'aria-hidden': 'true' });
  const namesG = svg('g', { class: 'places-map__names', 'aria-hidden': 'true' });  // nomi sopra tutti i cerchi
  const dots = [];   // { circle, hit, text, r0 } per aggiornare le dimensioni con lo zoom
  const labelBoxes = [];
  clusters.forEach((c, i) => {
    const r0 = 6 + 26 * Math.sqrt(c.count / maxCount);
    const cx = x(c.lon), cy = y(c.lat);
    const circle = svg('circle', { class: 'places-map__circle' });
    const hit = svg('circle', { class: 'places-map__hit' });
    const kids = [svg('title', {}, `${c.name} (${plural(c.count, 'foto', 'foto')})`), hit, circle];
    let text = null, above = false;
    // Nome (solo città, senza complementi) sui cerchi più grandi: sotto il cerchio, o sopra se sotto si sovrappone.
    if (i < NAME_LABELS) {
      const short = c.city || c.name;
      const w = short.length * LABEL_PX * 0.6, h = LABEL_PX * 1.2;
      const box = (up) => {
        const top = up ? cy - r0 - 2 - h : cy + r0 + 2;
        return { left: cx - w / 2, right: cx + w / 2, top, bottom: top + h };
      };
      const clashes = (b) => labelBoxes.some((o) => b.left < o.right && b.right > o.left && b.top < o.bottom && b.bottom > o.top);
      const pick = [false, true].map((up) => ({ up, b: box(up) })).find((p) => !clashes(p.b));
      if (pick) {
        labelBoxes.push(pick.b);
        above = pick.up;
        text = svg('text', { class: 'places-map__name' }, short);
        namesG.append(svg('g', { transform: `translate(${cx.toFixed(1)} ${cy.toFixed(1)})` }, text));
      }
    }
    const g = svg('g', {
      class: 'places-map__dot', 'data-index': i,
      transform: `translate(${cx.toFixed(1)} ${cy.toFixed(1)})`,
    }, ...kids);
    g.addEventListener('click', () => onSelect(i));
    dotsG.append(g);
    dots.push({ circle, hit, text, r0, above });
  });

  const el = svg('svg', {
    class: 'places-map__svg', viewBox: `0 0 ${MAP_W} ${H.toFixed(0)}`,
    role: 'group', 'aria-label': 'Mappa dei luoghi con foto',
  }, ...land, ...grid, dotsG, namesG);

  const setSelected = (index) => {
    dotsG.querySelectorAll('.places-map__dot').forEach((g) => {
      g.classList.toggle('is-selected', Number(g.getAttribute('data-index')) === index);
    });
  };
  // ---- vista: trascinamento, zoom con rotella e pulsanti (viewBox, senza ridisegnare)
  const base = { x: 0, y: 0, w: MAP_W, h: H };
  const vb = { ...base };
  const MIN_W = base.w / 12;   // zoom massimo
  // Ad ogni zoom cerchi, area toccabile e nomi tornano alla stessa dimensione sullo schermo
  // (le unita' del viewBox crescono/diminuiscono con lo zoom, i pixel no).
  const apply = () => {
    el.setAttribute('viewBox', `${vb.x.toFixed(1)} ${vb.y.toFixed(1)} ${vb.w.toFixed(1)} ${vb.h.toFixed(1)}`);
    const screenW = el.getBoundingClientRect().width || MAP_W;
    const unitPx = vb.w / screenW;          // unita' del viewBox per pixel schermo
    const zoomK = vb.w / base.w;            // 1 = vista completa
    for (const d of dots) {
      const r = d.r0 * zoomK;
      d.circle.setAttribute('r', r.toFixed(2));
      d.hit.setAttribute('r', Math.max(r, MIN_HIT_PX * unitPx).toFixed(2));
      if (d.text) {
        d.text.setAttribute('font-size', (LABEL_PX * unitPx).toFixed(2));
        // baseline: sotto il cerchio (la parte alta della lettera resta sotto il bordo) o sopra (lettere sopra il bordo)
        const gap = r + 2 * unitPx;
        d.text.setAttribute('y', (d.above ? -gap : gap + LABEL_PX * unitPx * 0.95).toFixed(2));
      }
    }
  };
  const clampView = () => {
    vb.x = Math.min(Math.max(vb.x, base.x), base.x + base.w - vb.w);
    vb.y = Math.min(Math.max(vb.y, base.y), base.y + base.h - vb.h);
  };
  const zoomAt = (factor, cx, cy) => {
    const nw = Math.min(base.w, Math.max(MIN_W, vb.w * factor));
    const nh = nw * (base.h / base.w);
    vb.x = cx - (cx - vb.x) * (nw / vb.w);
    vb.y = cy - (cy - vb.y) * (nh / vb.h);
    vb.w = nw; vb.h = nh;
    clampView(); apply();
  };
  const toView = (e) => {
    const r = el.getBoundingClientRect();
    return { x: vb.x + ((e.clientX - r.left) * vb.w) / r.width, y: vb.y + ((e.clientY - r.top) * vb.h) / r.height };
  };

  let drag = null;
  let suppressClick = false;
  // La cattura del puntatore si attiva solo quando si trascina: prima il clic arriva al cerchio toccato.
  el.addEventListener('pointerdown', (e) => {
    if (e.button !== 0) return;
    drag = { x: e.clientX, y: e.clientY, vx: vb.x, vy: vb.y, moved: false, id: e.pointerId };
  });
  el.addEventListener('pointermove', (e) => {
    if (!drag) return;
    if (!drag.moved) {
      if (Math.hypot(e.clientX - drag.x, e.clientY - drag.y) < 4) return;
      drag.moved = true;
      el.setPointerCapture?.(drag.id);
    }
    const r = el.getBoundingClientRect();
    const dx = ((e.clientX - drag.x) * vb.w) / r.width;
    const dy = ((e.clientY - drag.y) * vb.h) / r.height;
    vb.x = drag.vx - dx; vb.y = drag.vy - dy;
    clampView(); apply();
  });
  const endDrag = () => {
    if (drag?.moved) suppressClick = true;   // un trascinamento non deve aprire un luogo
    drag = null;
  };
  el.addEventListener('pointerup', endDrag);
  el.addEventListener('pointercancel', endDrag);
  // in fase di cattura: blocca il clic sul cerchio solo dopo un trascinamento
  el.addEventListener('click', (e) => {
    if (suppressClick) { suppressClick = false; e.stopPropagation(); e.preventDefault(); }
  }, true);
  // Rotella: zoom solo con Ctrl (o Cmd) premuto, altrimenti la pagina scorre normalmente.
  el.addEventListener('wheel', (e) => {
    if (!(e.ctrlKey || e.metaKey)) return;
    e.preventDefault();
    const pt = toView(e);
    zoomAt(e.deltaY > 0 ? 1.25 : 0.8, pt.x, pt.y);
  }, { passive: false });
  // Le dimensioni in pixel dipendono dalla larghezza dello schermo: si ricalcolano al ridimensionamento.
  if (typeof ResizeObserver !== 'undefined') new ResizeObserver(() => apply()).observe(el);
  apply();

  const btn = (label, ic, onClick) => h('md-icon-button', { 'aria-label': label, title: label, onclick: onClick }, icon(ic));
  const controls = h('div', { class: 'places-map__controls' },
    btn('Avvicina', 'add', () => zoomAt(0.7, vb.x + vb.w / 2, vb.y + vb.h / 2)),
    btn('Allontana', 'remove', () => zoomAt(1 / 0.7, vb.x + vb.w / 2, vb.y + vb.h / 2)),
    btn('Vista completa', 'center_focus_strong', () => { Object.assign(vb, base); apply(); }));

  return { el: h('div', { class: 'places-map' }, el, controls), setSelected };
}

export async function mount(root, { navigate, signal } = {}) {
  loadCss();
  const ac = new AbortController();
  const stop = () => ac.abort();
  signal?.addEventListener('abort', stop);
  const sig = ac.signal;
  const narrow = window.matchMedia('(max-width: 839px)');
  const reduceMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;

  let disposed = false;
  let precision = 1;
  let clusters = [];
  let selected = -1;       // indice del cluster aperto nel dettaglio
  let cardsShown = CARDS_PAGE;
  let map = null;
  let cardGrid = null;
  let moreBtn = null;
  let detail = null;       // { cluster, items, total, ac, grid, more, meta }
  let loadSeq = 0;

  // ---- intestazione e controlli (statici)
  const header = h('div', { class: 'view-header' },
    h('div', {},
      h('h1', { class: 'md-typescale-headline-medium' }, 'Luoghi'),
      h('p', { class: 'muted md-typescale-body-medium' }, 'Dove sono state scattate le tue foto')));
  const countLine = h('p', { class: 'places-count muted md-typescale-body-small', 'aria-live': 'polite' });
  const chips = filterChips(PRECISION, { selected: precision, label: 'Precisione', onChange: (p) => setPrecision(p) });
  [...chips.children].forEach((chip, i) => { chip.title = PRECISION[i].hint; });
  const toolbar = h('div', { class: 'places-toolbar' },
    chips,
    countLine);

  // ---- dettaglio (colonna accanto alla mappa su schermo largo, sotto su quello stretto)
  const detailHost = h('section', { class: 'places-detail places-detail--idle', 'aria-label': 'Foto del luogo selezionato' });
  const body = h('div', { class: 'places-body' });
  const peopleSlot = h('div', { class: 'places-people-slot' });
  root.replaceChildren(h('div', { class: 'view places-view' }, header, toolbar, body, peopleSlot));
  body.replaceChildren(skeletonCard(), skeletonGrid(12, { tile: 160 }));

  function setIdle() {
    detailHost.classList.add('places-detail--idle');
    detailHost.replaceChildren(h('div', { class: 'places-detail__idle card-outlined' },
      deco('touch_app'),
      h('p', { class: 'muted md-typescale-body-medium' }, 'Tocca un luogo sulla mappa o nell’elenco per vedere le foto scattate lì.')));
  }

  function markCards() {
    cardGrid?.querySelectorAll('.places-card').forEach((b) => {
      const on = Number(b.getAttribute('data-index')) === selected;
      b.classList.toggle('is-selected', on);
      b.setAttribute('aria-pressed', String(on));
    });
  }

  function closeDetail() {
    detail?.ac.abort();
    detail = null;
    selected = -1;
    map?.setSelected(-1);
    markCards();
    setIdle();
  }

  function cardFor(c, i) {
    return h('button', {
      class: 'card card--clickable places-card', type: 'button',
      'data-index': i, 'aria-pressed': 'false', onclick: () => selectCluster(i),
    },
      h('div', { class: 'places-card__media' }, thumbImg(c.sample_id, 256, '')),
      h('div', { class: 'places-card__body' },
        h('p', { class: 'places-card__title md-typescale-title-small' }, c.name),
        h('p', { class: 'muted md-typescale-body-small' }, plural(c.count, 'foto', 'foto')),
        c.showCoords ? h('p', { class: 'places-card__coords muted md-typescale-body-small' }, c.label) : null));
  }

  function renderCards() {
    cardGrid.replaceChildren(...clusters.slice(0, cardsShown).map(cardFor));
    moreBtn.hidden = cardsShown >= clusters.length;
    moreBtn.textContent = `Mostra altri (${clusters.length - cardsShown})`;
    markCards();
  }

  // ---- dettaglio di un luogo: griglia dei file, paginata, con visualizzatore
  async function loadDetailPage(d) {
    if (d.items.length && d.items.length >= d.total) return [];
    const start = d.items.length;
    try {
      const page = await api.get('/places/files', {
        lat: d.cluster.lat, lon: d.cluster.lon, precision, offset: start, limit: FILES_PAGE,
      }, d.ac.signal);
      if (disposed || detail !== d) return [];
      d.total = page.total;
      d.items.push(...page.items);
      if (start === 0) d.grid.replaceChildren();
      page.items.forEach((f, k) => {
        d.grid.append(createTile(f, { size: 110, onOpen: () => openFileViewer(d, start + k) }));
      });
      if (!d.items.length) {
        d.grid.replaceChildren(h('p', { class: 'muted md-typescale-body-small' }, 'Nessuna foto in questo luogo.'));
      }
      d.more.hidden = d.items.length >= d.total;
      d.meta.textContent = `${plural(d.total, 'foto', 'foto')} in questo luogo`;
      return page.items;
    } catch (e) {
      if (disposed || e.name === 'AbortError' || detail !== d) return [];
      showSnackbar(e.message, { error: true });
      if (start === 0) d.grid.replaceChildren(errorState({ title: 'Impossibile caricare le foto', message: e.message }));
      return [];
    }
  }

  async function loadMoreDetail(d) {
    d.more.disabled = true;
    await loadDetailPage(d);
    if (detail === d) d.more.disabled = false;
  }

  async function openFileViewer(d, index) {
    try {
      const { openViewer } = await import('./viewer.js');
      openViewer({ items: [...d.items], index, loadMore: () => loadDetailPage(d) });
    } catch {
      showSnackbar('Il visualizzatore non è disponibile.', { error: true });
    }
  }

  function openDetail(c) {
    const d = { cluster: c, items: [], total: c.count, ac: new AbortController() };
    detail = d;
    d.meta = h('p', { class: 'muted md-typescale-body-small', 'aria-live': 'polite' }, `${plural(c.count, 'foto', 'foto')} in questo luogo`);
    d.grid = h('div', { class: 'tile-grid places-detail__grid' }, skeletonGrid(12, { tile: 110 }));
    d.more = h('md-text-button', { class: 'places-detail__more', hidden: true, onclick: () => loadMoreDetail(d) }, 'Carica altre foto');
    const osm = h('a', {
      class: 'places-osm', href: `https://www.openstreetmap.org/?mlat=${c.lat}&mlon=${c.lon}&zoom=12`,
      target: '_blank', rel: 'noopener',
    }, deco('open_in_new'), 'Apri in OpenStreetMap');

    detailHost.classList.remove('places-detail--idle');
    detailHost.replaceChildren(
      h('div', { class: 'places-detail__head' },
        h('div', { class: 'places-detail__copy' },
          h('h2', { class: 'places-detail__title md-typescale-title-medium' }, c.name),
          d.meta,
          osm),
        h('md-icon-button', { 'aria-label': 'Chiudi', onclick: closeDetail }, deco('close'))),
      d.grid,
      d.more);
    loadDetailPage(d);
  }

  function selectCluster(i) {
    const c = clusters[i];
    if (!c) return;
    if (selected === i) { closeDetail(); return; }
    closeDetail();
    selected = i;
    map.setSelected(i);
    if (i >= cardsShown) {
      while (cardsShown <= i) cardsShown += CARDS_PAGE;
      renderCards();
    }
    markCards();
    openDetail(c);
    // Su schermo stretto il dettaglio e' alto quanto lo schermo: mostriamo l'intestazione (titolo e chiudi), non la coda.
    detailHost.scrollIntoView({ block: narrow.matches ? 'start' : 'nearest', behavior: reduceMotion ? 'auto' : 'smooth' });
  }

  function lowCallout(n) {
    return h('div', { class: 'callout callout--info places-callout', role: 'note' },
      deco('info', 'callout__icon'),
      h('div', {},
        h('p', { class: 'callout__title' }, 'Poche foto con posizione'),
        h('p', { class: 'md-typescale-body-medium' },
          `Solo ${plural(n, 'foto ha', 'foto hanno')} la posizione. Le foto senza GPS non compaiono qui.`)));
  }

  function renderPlaces() {
    const total = clusters.reduce((s, c) => s + c.count, 0);
    countLine.textContent = clusters.length
      ? `${plural(clusters.length, 'luogo', 'luoghi')} · ${plural(total, 'foto con posizione', 'foto con posizione')}`
      : '';
    if (!total) {
      body.replaceChildren(emptyState({
        icon: 'location_off',
        title: 'Nessuna foto con posizione',
        message: 'Le foto compaiono qui solo se contengono il dato di posizione (GPS). Le altre non vengono mostrate.',
      }));
      return;
    }

    // Confini e città sono gia' pronti (vedi load): la mappa si costruisce una sola volta.
    map = buildMap(clusters, selectCluster, worldData);
    cardGrid = h('div', { class: 'grid-auto places-grid', 'aria-label': 'Luoghi' });
    moreBtn = h('md-text-button', {
      class: 'places-more',
      onclick: () => { cardsShown += CARDS_PAGE; renderCards(); },
    });

    body.replaceChildren(
      ...(total < LOW_GPS ? [lowCallout(total)] : []),
      h('div', { class: 'places-overview' },
        h('section', { class: 'section places-map-section' },
          h('h2', { class: 'section-title md-typescale-title-medium' }, 'Mappa'),
          map.el,
          h('p', { class: 'muted md-typescale-body-small places-legend' },
            'Più il cerchio è grande, più foto ci sono. Toccalo per vedere le foto. Ctrl + rotellina per ingrandire.')),
        detailHost),
      h('section', { class: 'section places-list-section' },
        h('h2', { class: 'section-title md-typescale-title-medium' }, 'Luoghi'),
        cardGrid,
        moreBtn));
    setIdle();
    renderCards();
  }

  async function load() {
    const seq = ++loadSeq;
    closeDetail();
    cardsShown = CARDS_PAGE;
    body.replaceChildren(skeletonCard(), skeletonGrid(12, { tile: 160 }));
    try {
      const [data] = await Promise.all([api.get('/places', { precision }, sig), loadCities(), loadWorld()]);
      if (disposed || seq !== loadSeq) return;
      clusters = labelClusters(data.map((c) => {
        const place = placeOf(c);
        return { ...c, ...place, title: place.name };
      }));
      renderPlaces();
    } catch (e) {
      if (disposed || e.name === 'AbortError') return;
      showSnackbar(e.message, { error: true });
      body.replaceChildren(errorState({ title: 'Impossibile caricare i luoghi', message: e.message, onRetry: load }));
    }
  }

  function setPrecision(p) {
    if (p === precision) return;
    precision = p;
    load();
  }

  // ---- persone: nascosta se vuota, avviso se il riconoscimento volti e' spento
  function renderFacesOff() {
    peopleSlot.replaceChildren(h('section', { class: 'section places-people', 'aria-labelledby': 'places-people-title' },
      h('h2', { id: 'places-people-title', class: 'section-title md-typescale-title-medium' }, 'Persone'),
      h('article', { class: 'card-outlined places-faces-off' },
        deco('face', 'places-faces-off__icon'),
        h('div', { class: 'places-faces-off__copy' },
          h('h3', { class: 'md-typescale-title-small' }, 'Riconoscimento volti non attivo'),
          h('p', { class: 'muted md-typescale-body-medium' },
            'Attivalo nelle impostazioni per raggruppare le persone che compaiono nelle foto.'),
          h('md-text-button', { onclick: () => navigate('/settings', { tab: 'general' }) }, 'Apri impostazioni')))));
  }

  function renderPeople(people) {
    const preview = people.slice(0, 8);
    peopleSlot.replaceChildren(h('section', { class: 'section places-people', 'aria-labelledby': 'places-people-title' },
      h('div', { style: 'display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap;' },
        h('h2', { id: 'places-people-title', class: 'section-title md-typescale-title-medium', style: 'margin:0;' }, 'Persone'),
        h('md-text-button', { onclick: () => navigate('/people') }, 'Vedi tutte')),
      h('div', { class: 'grid-auto places-people__grid' },
        preview.map((p) => {
          const label = p.name || 'Senza nome';
          const img = p.cover_face_id
            ? h('img', {
                src: fileUrl.faceCrop(p.cover_face_id, 256),
                alt: label, loading: 'lazy', decoding: 'async',
                onerror: (e) => {
                  if (p.sample_file_id && !e.target.dataset.fb) {
                    e.target.dataset.fb = '1';
                    e.target.src = fileUrl.thumb(p.sample_file_id, 256);
                  } else {
                    e.target.replaceWith(h('div', { class: 'places-fallback', 'aria-hidden': 'true' }, icon('face')));
                  }
                },
              })
            : thumbImg(p.sample_file_id, 256, label);
          return h('button', {
            type: 'button', class: 'card card--clickable places-person',
            onclick: () => navigate('/people', { id: p.id }),
          },
            h('div', { class: 'places-person__media' }, img),
            h('div', { class: 'places-person__body' },
              h('p', { class: 'md-typescale-title-small' }, label),
              h('p', { class: 'muted md-typescale-body-small' }, plural(p.count, 'foto', 'foto'))));
        })),
      h('p', { class: 'muted md-typescale-body-small places-people__caption' }, 'Raggruppate localmente dal riconoscimento volti.')));
  }

  async function loadPeople() {
    const [cfg, people] = await Promise.allSettled([
      api.get('/settings', null, sig),
      api.get('/people', null, sig),
    ]);
    if (disposed) return;
    if (cfg.status === 'rejected') {
      showSnackbar(cfg.reason.message, { error: true });
      return;
    }
    if (!cfg.value.enable_faces) { renderFacesOff(); return; }
    if (people.status === 'rejected') {
      showSnackbar(people.reason.message, { error: true });
      return;
    }
    if (people.value.length) renderPeople(people.value);
  }

  load();
  loadPeople();

  return () => {
    disposed = true;
    signal?.removeEventListener('abort', stop);
    ac.abort();
    detail?.ac.abort();
    detail = null;
  };
}
