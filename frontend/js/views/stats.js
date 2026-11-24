// Statistiche dell'archivio: grafici SVG scritti a mano (nessuna libreria). Dati: GET /stats/overview.
// Colori solo dai token M3 (classi .stats-tone--* in css/view-stats.css). Ogni grafico ha role=img,
// un'etichetta descrittiva e una tabella alternativa ("Mostra come tabella").
import { h, icon } from '../ui/el.js';
import { api } from '../api.js';
import { emptyState, errorState } from '../ui/empty-state.js';
import { skeletonCard } from '../ui/skeleton.js';
import { formatBytes, formatDay, formatNumber, plural } from '../ui/format.js';

const NS = 'http://www.w3.org/2000/svg';
const ORDER = ['exif', 'sidecar', 'filename', 'video', 'mtime', 'nessuna'];
const SOURCE_INFO = {
  exif: ['Dati EXIF della foto', 'Data scritta dalla fotocamera o dal telefono dentro il file: la più affidabile.'],
  sidecar: ['File JSON di Google Foto', 'Il file .json esportato accanto alla foto, con la data di scatto.'],
  filename: ['Nome del file', 'Data riconoscibile nel nome, come IMG_20200609_…'],
  video: ['Dati del video', 'Data registrata nel contenitore del video.'],
  mtime: ['Data di modifica del file', 'Ultima risorsa: può non corrispondere allo scatto.'],
  nessuna: ['Nessuna data', 'Nessuna data utilizzabile trovata.'],
};
const DATES_TEXT = 'Per ogni file cerchiamo la data di scatto in quest’ordine: metadati della foto, file di Google Foto, '
  + 'nome del file. Se non trovi nulla, usiamo la data di modifica, che è la meno affidabile.';

let cssLoaded = false;
function loadCss() {
  if (cssLoaded || document.querySelector('link[data-view="stats"]')) { cssLoaded = true; return; }
  cssLoaded = true;
  const l = document.createElement('link');
  l.rel = 'stylesheet'; l.href = 'css/view-stats.css'; l.dataset.view = 'stats';
  document.head.append(l);
}

let uid = 0;

/** Elemento SVG con attributi (null/false/undefined saltati). */
function svgEl(tag, attrs = {}, ...kids) {
  const el = document.createElementNS(NS, tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v !== undefined && v !== null && v !== false) el.setAttribute(k, String(v));
  }
  el.append(...kids);
  return el;
}

/** Percentuale intera in tutta la vista; sotto l'1 % una dicitura unica invece di "0,0 %". */
const pct = (v, t) => {
  if (!t) return '0 %';
  const p = (v / t) * 100;
  return p < 1 && v > 0 ? '< 1 %' : `${Math.round(p)} %`;
};
/** Anni consecutivi tra il primo e l'ultimo, con 0 per gli anni senza dati (asse temporale continuo). */
function fillYears(years) {
  const byYear = new Map(years.map((y) => [y.year, y]));
  const out = [];
  for (let y = years[0].year; y <= years[years.length - 1].year; y++) {
    out.push(byYear.get(y) || { year: y, photos: 0, videos: 0 });
  }
  return out;
}
const sum = (list, f) => list.reduce((s, x) => s + f(x), 0);

/** Scala "pulita" per l'asse Y: 4 intervalli di passo 1/2/2,5/5/10 × potenza di 10. */
function niceScale(max) {
  const raw = Math.max(max, 4) / 4;
  const pow = 10 ** Math.floor(Math.log10(raw));
  // Passi non interi (2,5 con potenza 1) darebbero etichette arrotondate male: si saltano.
  const step = [1, 2, 2.5, 5, 10].map((m) => m * pow).find((s) => s >= raw && Number.isInteger(s));
  return { step, max: step * 4 };
}

function kpiCard(value, label, sub, ic = null, tone = 'primary') {
  return h('div', { class: `card stat-card stats-kpi stats-kpi--${tone}` },
    ic ? h('span', { class: 'stats-kpi__icon', 'aria-hidden': 'true' }, icon(ic)) : null,
    h('div', { class: 'stats-kpi__body' },
      h('span', { class: 'stat-card__label' }, label),
      h('span', { class: 'stat-card__value tabular' }, value),
      sub ? h('span', { class: 'stats-kpi__sub muted md-typescale-body-small' }, sub) : null));
}

function noData(text) {
  return h('p', { class: 'muted md-typescale-body-medium' }, text);
}

function legend(items, extraClass = '') {
  return h('ul', { class: `stats-legend md-typescale-body-medium ${extraClass}`.trim() },
    items.map((i) => h('li', {}, h('span', { class: `stats-swatch stats-tone--${i.tone}`, 'aria-hidden': 'true' }), i.label)));
}

function dataTable(caption, heads, rows) {
  return h('div', { class: 'stats-table-wrap' },
    h('table', { class: 'table-m3' },
      h('caption', { class: 'visually-hidden' }, caption),
      h('thead', {}, h('tr', {}, heads.map((t, i) => h('th', { scope: 'col', class: i ? 'num' : '' }, t)))),
      h('tbody', {}, rows.map((r) => h('tr', {}, r.map((c, i) => h('td', { class: i ? 'num' : '' }, c)))))));
}

/** Pulsante che alterna grafico e tabella. */
function toggleButton(chart, table) {
  const label = h('span', {}, 'Mostra come tabella');
  const ico = icon('table_chart', { slot: 'icon' });
  table.hidden = true;
  const btn = h('md-text-button', {
    class: 'stats-toggle',
    onclick: () => {
      const showTable = table.hidden;
      table.hidden = !showTable;
      chart.hidden = showTable;
      label.textContent = showTable ? 'Mostra come grafico' : 'Mostra come tabella';
      // L'icona indica la vista che si ottiene al tocco: tabella = table_chart, grafico = bar_chart.
      ico.firstChild.textContent = showTable ? 'bar_chart' : 'table_chart';
    },
  }, ico, label);
  return btn;
}

function panel({ title, desc, badge = null, chart, table, wide = false, cls = '' }) {
  const id = `stats-h-${++uid}`;
  const cl = ['card', 'stats-panel', wide ? 'stats-panel--wide' : '', cls].filter(Boolean).join(' ');
  return h('section', { class: cl, 'aria-labelledby': id },
    h('div', { class: 'stats-panel__head' },
      h('div', { class: 'stats-panel__titles' },
        h('div', { class: 'stats-panel__title-row' },
          h('h2', { id, class: 'md-typescale-title-large' }, title),
          badge ? h('span', { class: 'stats-badge tabular' }, badge) : null),
        desc ? h('p', { class: 'muted md-typescale-body-medium' }, desc) : null),
      table ? toggleButton(chart, table) : null),
    h('div', { class: 'stats-panel__body' }, chart, table));
}

/** Barre orizzontali (SVG con scala fissa, riempimento responsive) con etichetta e valore sopra. */
function hbars({ items, total, tone, title, ui }) {
  const max = Math.max(1, ...items.map((i) => i.count));
  const summary = items.map((i) => `${i.name} ${formatNumber(i.count)} (${pct(i.count, total)})`).join('; ');
  return h('div', { class: 'stats-hbars', role: 'group', 'aria-label': `${title}: ${summary}` },
    items.map((it, idx) => {
      const share = pct(it.count, total);
      const row = h('div', {
        class: 'stats-hrow', tabindex: 0,
        'aria-label': `${it.name}: ${formatNumber(it.count)} elementi, ${share}`,
      },
        h('div', { class: 'stats-hrow__head' },
          h('span', { class: 'stats-hrow__name md-typescale-body-medium' }, it.name),
          h('span', { class: 'stats-hrow__value tabular md-typescale-label-large' }, `${formatNumber(it.count)} · ${share}`)),
        it.note ? h('span', { class: 'stats-hrow__note md-typescale-body-small' }, it.note) : null,
        svgEl('svg', {
          class: 'stats-hbar', viewBox: '0 0 1000 12', preserveAspectRatio: 'none',
          focusable: 'false', 'aria-hidden': 'true', style: `--d:${idx * 30}ms`,
        },
        svgEl('rect', { class: 'stats-hbar__track', x: 0, y: 0, width: 1000, height: 12 }),
        svgEl('rect', { class: `stats-hbar__fill stats-tone--${tone}`, x: 0, y: 0, width: (it.count / max) * 1000, height: 12 })));
      ui.tipOn(row, () => [it.name, `${formatNumber(it.count)} elementi · ${share}`]);
      return row;
    }));
}

/** Grafico per anno. Disegnato 1:1 in pixel (viewBox = dimensioni reali): il testo resta a 11-12 px
 *  e, se gli anni non entrano nella larghezza disponibile, la sola area grafico scorre in orizzontale. */
function yearsSvg(years, ui, availW) {
  const n = years.length;
  const padL = 52, padR = 18, padT = 26, padB = 32, ph = 220;
  const fitSlot = (availW - padL - padR) / n;
  const slot = Math.max(24, fitSlot);
  const W = Math.round(padL + padR + slot * n), H = padT + ph + padB, base = padT + ph;
  const totals = years.map((y) => y.photos + y.videos);
  const sc = niceScale(Math.max(...totals));
  const hOf = (v) => (v / sc.max) * ph;
  const bw = Math.min(28, Math.max(10, slot * 0.62));
  const peak = totals.indexOf(Math.max(...totals));
  const compactAxis = slot < 36;
  const fmtVal = (v) => (v >= 1000 && slot < 34 ? `${(v / 1000).toFixed(1).replace('.', ',')}k` : formatNumber(v));
  const svg = svgEl('svg', {
    class: 'stats-chart stats-chart--years', width: W, height: H, viewBox: `0 0 ${W} ${H}`, role: 'group',
    'aria-label': `Foto e video per anno, dal ${years[0].year} al ${years[n - 1].year}. `
      + `L’anno con più elementi è il ${years[peak].year}, con ${formatNumber(totals[peak])}.`,
  });
  for (let k = 0; k <= 4; k++) {
    const yy = base - hOf(sc.step * k);
    svg.append(
      svgEl('line', { class: 'stats-grid-line', x1: padL, x2: W - padR, y1: yy, y2: yy }),
      svgEl('text', { class: 'stats-axis', x: padL - 8, y: yy + 4, 'text-anchor': 'end' }, formatNumber(sc.step * k)));
  }
  const valueLabels = [];
  years.forEach((y, i) => {
    const tot = totals[i], cx = padL + slot * (i + 0.5), x = cx - bw / 2;
    const hp = hOf(y.photos), hv = hOf(y.videos), delay = `--d:${i * 25}ms`;
    const valueEl = svgEl('text', { class: 'stats-value', x: cx, y: base - hOf(tot) - 6, 'text-anchor': 'middle' }, tot > 0 || !compactAxis ? fmtVal(tot) : '');
    valueLabels.push(valueEl);
    const showYear = !compactAxis || i === 0 || i === n - 1 || (i % 2 === 0 && i !== n - 2);
    svg.append(
      svgEl('rect', { class: 'stats-bar stats-tone--photo', x, y: base - hp, width: bw, height: hp, rx: 3, style: delay }),
      svgEl('rect', { class: 'stats-bar stats-tone--video', x, y: base - hp - hv, width: bw, height: hv, rx: 3, style: delay }),
      valueEl,
      showYear ? svgEl('text', { class: 'stats-axis', x: cx, y: base + 20, 'text-anchor': 'middle' }, String(y.year)) : null);
  });
  // Colonne trasparenti sopra i dati: area di hover e di focus da tastiera.
  // Il tooltip si aggancia all'etichetta del totale, cioe' sopra la barra.
  years.forEach((y, i) => {
    const col = svgEl('rect', {
      class: 'stats-col', role: 'img', tabindex: 0,
      x: padL + slot * i, y: padT - 8, width: slot, height: H - padT + 8,
      'aria-label': `${y.year}: ${formatNumber(y.photos)} foto, ${formatNumber(y.videos)} video`,
    });
    ui.tipOn(col, () => [String(y.year), `${formatNumber(y.photos)} foto`, `${formatNumber(y.videos)} video`,
      `Totale ${formatNumber(y.photos + y.videos)}`], valueLabels[i]);
    svg.append(col);
  });
  return svg;
}

function yearsPanel(years, ui) {
  const title = 'Foto e video per anno';
  const desc = 'Distribuzione cronologica del tuo archivio, anno per anno.';
  if (!years.length) return panel({ title, desc, chart: noData('Nessuna data valida da mostrare.'), wide: true });
  const full = fillYears(years);
  const peak = full.reduce((best, y) => ((y.photos + y.videos) > (best.photos + best.videos) ? y : best), full[0]);
  const peakTot = peak.photos + peak.videos;
  const badge = peakTot > 0 ? `Picco: ${peak.year} (${formatNumber(peakTot)})` : null;
  const chart = h('div', { class: 'stats-panel__chart' },
    legend([{ tone: 'photo', label: 'Foto' }, { tone: 'video', label: 'Video' }]),
    h('div', {
      class: 'stats-scroll', tabindex: 0, role: 'region', 'aria-label': 'Grafico scorrevole: foto e video per anno',
      onscroll: ui.hideTip,
    }, yearsSvg(full, ui, ui.availWidth())));
  const table = dataTable(title, ['Anno', 'Foto', 'Video', 'Totale'],
    full.map((y) => [String(y.year), formatNumber(y.photos), formatNumber(y.videos), formatNumber(y.photos + y.videos)]));
  return panel({ title, desc, badge, chart, table, wide: true });
}

function contextPanel(list, ui) {
  const title = 'Contesti riconosciuti';
  if (!list.length) return panel({ title, desc: 'Di che cosa parlano le foto.', chart: noData('Nessun contesto riconosciuto ancora.') });
  const total = sum(list, (i) => i.count);
  // Le quote sono sul totale dei soli elementi con un contesto (senza duplicati), non su tutto l'archivio.
  const desc = `Di che cosa parlano le foto. Quote calcolate su ${formatNumber(total)} elementi con un contesto riconosciuto (senza duplicati).`;
  const items = list.map((i) => ({ name: i.context, count: i.count }));
  const chart = hbars({ items, total, tone: 'context', title, ui });
  const table = dataTable(title, ['Contesto', 'Elementi', 'Quota'],
    items.map((i) => [i.name, formatNumber(i.count), pct(i.count, total)]));
  return panel({ title, desc, chart, table });
}

function donutPanel(sizes, photos, videos, ui) {
  const title = 'Foto e video, per peso';
  const desc = 'Quanto spazio occupano: a volte pochi video pesano più di tante foto.';
  const pb = sizes.image || 0, vb = sizes.video || 0, tb = pb + vb;
  if (!tb) return panel({ title, desc, chart: noData('Nessun file con dimensione.') });
  const R = 78, SW = 24, C = 2 * Math.PI * R;
  const lenP = (pb / tb) * C;
  const parts = [
    { cls: 'photo', name: 'Foto', len: lenP, off: 0, bytes: pb, count: photos },
    { cls: 'video', name: 'Video', len: C - lenP, off: lenP, bytes: vb, count: videos },
  ];
  const ring = svgEl('g', { transform: 'rotate(-90 100 100)' },
    svgEl('circle', { class: 'stats-donut__track', cx: 100, cy: 100, r: R, 'stroke-width': SW }));
  for (const p of parts) {
    const seg = svgEl('circle', {
      class: `stats-seg stats-seg--${p.cls}`, cx: 100, cy: 100, r: R, 'stroke-width': SW,
      tabindex: 0, role: 'img', 'aria-label': `${p.name}: ${formatBytes(p.bytes)}`,
      style: `--len:${p.len};--c:${C};stroke-dashoffset:${-p.off}`,
    });
    ui.tipOn(seg, () => [p.name, `${formatBytes(p.bytes)} · ${pct(p.bytes, tb)}`, `${formatNumber(p.count)} elementi`]);
    ring.append(seg);
  }
  const svg = svgEl('svg', {
    class: 'stats-donut__svg', viewBox: '0 0 200 200', role: 'group', focusable: 'false',
    'aria-label': `Peso per tipo: foto ${formatBytes(pb)} (${pct(pb, tb)}), video ${formatBytes(vb)} (${pct(vb, tb)}). `
      + `Totale ${formatBytes(tb)}.`,
  }, ring);
  const donut = h('div', { class: 'stats-donut' }, svg,
    h('div', { class: 'stats-donut__center' },
      h('strong', { class: 'md-typescale-title-large tabular' }, formatBytes(tb)),
      h('span', { class: 'md-typescale-label-medium muted' }, 'spazio totale')));
  const legendEl = h('ul', { class: 'stats-legend stats-legend--stack md-typescale-body-medium' }, parts.map((p) =>
    h('li', {},
      h('span', { class: `stats-swatch stats-tone--${p.cls}`, 'aria-hidden': 'true' }),
      h('span', { class: 'stats-legend__text' },
        h('strong', {}, `${p.name} · ${pct(p.bytes, tb)}`),
        h('span', { class: 'muted' }, `${formatBytes(p.bytes)} · ${plural(p.count, 'elemento', 'elementi')}`)))));
  const chart = h('div', { class: 'stats-donut-row' }, donut, legendEl);
  const table = dataTable(title, ['Tipo', 'Elementi', 'Spazio', 'Quota'],
    parts.map((p) => [p.name, formatNumber(p.count), formatBytes(p.bytes), pct(p.bytes, tb)]));
  return panel({ title, desc, chart, table, cls: 'stats-panel--donut' });
}

function datesPanel(sources, ui) {
  const title = 'Come abbiamo trovato le date';
  const rank = (s) => { const i = ORDER.indexOf(s); return i < 0 ? 99 : i; };
  const list = [...sources].sort((a, b) => rank(a.source) - rank(b.source));
  if (!list.length) return panel({ title, desc: DATES_TEXT, chart: noData('Nessun dato sulle date.') });
  const total = sum(list, (s) => s.count);
  const items = list.map((s) => {
    const info = SOURCE_INFO[s.source] || [s.source, ''];
    return { name: info[0], note: info[1], count: s.count };
  });
  const chart = hbars({ items, total, tone: 'source', title, ui });
  const table = dataTable(title, ['Origine', 'Elementi', 'Quota'],
    items.map((i) => [i.name, formatNumber(i.count), pct(i.count, total)]));
  return panel({ title, desc: DATES_TEXT, chart, table });
}

function daysPanel(days, ui) {
  const title = 'Giorni più ricchi';
  const desc = 'I giorni con più foto e video. Toccane uno per vederlo in galleria.';
  if (!days.length) return panel({ title, desc, chart: noData('Nessuna data valida da mostrare.') });
  const max = Math.max(1, ...days.map((d) => d.count));
  const chart = h('ol', { class: 'stats-days' }, days.map((d, idx) => {
    const arrow = icon('chevron_right');
    arrow.setAttribute('aria-hidden', 'true');
    return h('li', {},
      h('button', {
        type: 'button', class: 'stats-day',
        'aria-label': `${formatDay(d.day)}: ${plural(d.count, 'elemento', 'elementi')}. Apri in galleria`,
        onclick: () => ui.navigate('/gallery', { day: d.day }),
      },
      h('span', { class: `stats-day__rank tabular${idx < 3 ? ' is-top' : ''}`, 'aria-hidden': 'true' }, String(idx + 1)),
      h('span', { class: 'stats-day__date md-typescale-body-medium' }, formatDay(d.day)),
      h('span', { class: 'stats-day__count tabular md-typescale-label-large' }, plural(d.count, 'elemento', 'elementi')),
      arrow,
      h('span', { class: 'stats-day__bar', 'aria-hidden': 'true' },
        h('span', { class: 'stats-day__fill', style: `--ratio:${((d.count / max) * 100).toFixed(1)}%` }))));
  }));
  // Il 1 gennaio con moltissimi elementi e' spesso una data predefinita, non uno scatto reale: solo un avviso.
  const suspect = days.some((d) => /-01-01$/.test(d.day) && d.count >= 200);
  const caveat = suspect
    ? h('p', { class: 'muted md-typescale-body-small stats-caveat' },
      'Alcuni elementi hanno come data il 1 gennaio: può essere una data predefinita e non lo scatto reale.')
    : null;
  return panel({ title, desc, chart: h('div', {}, chart, caveat) });
}

/** Costruisce il contenuto della pagina dai dati di /stats/overview. */
function buildBody(d, ui) {
  const kinds = Object.fromEntries(d.per_kind.map((k) => [k.kind, k.count]));
  const photos = kinds.image || 0, videos = kinds.video || 0, total = photos + videos;
  if (!total) {
    return [emptyState({
      icon: 'bar_chart', title: 'Ancora nessuna statistica',
      message: 'Quando le foto saranno indicizzate, qui vedrai i numeri del tuo archivio.',
      actionLabel: 'Vai all’indicizzazione', onAction: () => ui.navigate('/index'),
    })];
  }
  const years = d.per_year;
  const kpis = h('div', { class: 'stats-kpis' },
    kpiCard(formatNumber(total), 'Elementi totali', 'foto e video analizzati', 'photo_library', 'primary'),
    kpiCard(formatNumber(photos), 'Foto', `${pct(photos, total)} del totale`, 'image', 'primary'),
    kpiCard(formatNumber(videos), 'Video', `${pct(videos, total)} del totale`, 'videocam', 'tertiary'),
    kpiCard(formatBytes(d.size_total), 'Spazio totale', 'peso su disco', 'hard_drive', 'secondary'),
    kpiCard(String(years.length), 'Anni coperti',
      years.length ? `dal ${years[0].year} al ${years[years.length - 1].year}` : 'nessuna data valida', 'calendar_month', 'secondary'));
  const grid = h('div', { class: 'stats-grid' },
    yearsPanel(years, ui),
    contextPanel(d.per_context, ui),
    donutPanel(d.size_by_kind, photos, videos, ui),
    datesPanel(d.date_sources, ui),
    daysPanel(d.top_days, ui));
  return [kpis, grid];
}

export async function mount(root, { navigate, signal } = {}) {
  loadCss();
  const ac = new AbortController();
  let disposed = false;

  const tip = h('div', { class: 'stats-tip', 'aria-hidden': 'true' });
  const body = h('div', { class: 'stats-body', 'aria-busy': 'true' });
  const view = h('div', { class: 'view stats-view' },
    h('div', { class: 'view-header' },
      h('div', {},
        h('h1', { class: 'md-typescale-headline-medium' }, 'Statistiche'),
        h('p', { class: 'muted md-typescale-body-medium' }, 'I numeri del tuo archivio: quante foto e video, da quando, dove.'))),
    body, tip);
  root.append(view);

  // Tooltip unico, posizionato rispetto alla vista (position: relative).
  const hideTip = () => tip.classList.remove('is-shown');
  const showTip = (anchor, lines) => {
    if (disposed) return;
    tip.replaceChildren(...lines.map((t, i) => h('div', { class: i ? 'stats-tip__sub' : 'stats-tip__title' }, t)));
    tip.classList.add('is-shown');
    const a = anchor.getBoundingClientRect(), c = view.getBoundingClientRect();
    const tw = tip.offsetWidth;
    const x = Math.min(Math.max(a.left + a.width / 2 - c.left, tw / 2 + 8), c.width - tw / 2 - 8);
    tip.style.left = `${x}px`;
    tip.style.top = `${a.top - c.top}px`;
  };
  // anchor: elemento a cui agganciare il tooltip (di default l'elemento stesso).
  const tipOn = (el, lines, anchor = el) => {
    el.addEventListener('pointerenter', () => showTip(anchor, lines()));
    el.addEventListener('pointerleave', hideTip);
    el.addEventListener('focus', () => showTip(anchor, lines()));
    el.addEventListener('blur', hideTip);
  };
  // Larghezza utile del grafico per anni (carta con padding), senza ridisegnare al resize.
  const availWidth = () => Math.max(320, view.clientWidth - 140);
  const ui = { tipOn, hideTip, availWidth, navigate: navigate || (() => {}) };

  // Il tooltip non deve restare acceso dopo un tocco fuori, o dopo uno scorrimento della pagina.
  const dismiss = (e) => { if (!e.target.closest?.('.stats-chart, .stats-hbars, .stats-donut')) hideTip(); };
  document.addEventListener('pointerdown', dismiss, { capture: true, signal: ac.signal });
  window.addEventListener('scroll', hideTip, { capture: true, passive: true, signal: ac.signal });

  const onAbort = () => { disposed = true; ac.abort(); };
  signal?.addEventListener('abort', onAbort);

  async function load() {
    body.setAttribute('aria-busy', 'true');
    // Scheletro con la stessa forma della pagina reale (riga KPI + pannelli), per non far saltare il layout.
    body.replaceChildren(
      h('div', { class: 'stats-kpis' }, Array.from({ length: 5 }, () => skeletonCard())),
      h('div', { class: 'stats-grid' }, Array.from({ length: 2 }, () => skeletonCard())));
    try {
      const data = await api.get('/stats/overview', undefined, ac.signal);
      if (disposed) return;
      body.replaceChildren(...buildBody(data, ui));
      body.setAttribute('aria-busy', 'false');
      // Animazione d'ingresso: dopo due frame, così lo stato iniziale e' gia' dipinto.
      requestAnimationFrame(() => requestAnimationFrame(() => body.classList.add('is-in')));
    } catch (e) {
      if (disposed || e.name === 'AbortError') return;
      body.setAttribute('aria-busy', 'false');
      // Un solo messaggio, in linguaggio semplice: il dettaglio tecnico non serve a chi usa l'app.
      body.replaceChildren(errorState({
        title: 'Statistiche non disponibili',
        message: 'Non riesco a leggere le statistiche. Controlla che l’app sia avviata e riprova.',
        onRetry: load,
      }));
    }
  }
  load();

  return () => {
    disposed = true;
    ac.abort();
    signal?.removeEventListener('abort', onAbort);
    tip.remove();
  };
}
