// Shell adattiva: sidebar (estesa o rail), top bar, barra del flusso "Organizza", bottom bar compatta e outlet del router.
// Non esistono md-navigation-bar/rail nel bundle: la navigazione e' costruita con md-icon, md-ripple e css/shell.css.
import { navItems, navGroups, flowSteps, organizeEntry, activeNavId, isFlowPath } from './nav.js';
import { store, applyTheme } from './store.js';
import { api, onEvent } from './api.js';
import { navigate, onRoute, currentRoute, prefetchRoute } from './router.js';
import { h, icon } from './ui/el.js';
import { createWordmark } from './ui/brand.js';
import { formatNumber } from './ui/format.js';

const MQ_MEDIUM = matchMedia('(min-width: 600px)');
const MQ_EXPANDED = matchMedia('(min-width: 1024px)');
const REDUCED = matchMedia('(prefers-reduced-motion: reduce)');

const THEME_ICON = { system: 'contrast', light: 'light_mode', dark: 'dark_mode' };
const THEME_NEXT = { system: 'light', light: 'dark', dark: 'system' };
const THEME_LABEL = { system: 'sistema', light: 'chiaro', dark: 'scuro' };
const GROUP_LABEL = Object.fromEntries(navGroups.map((g) => [g.id, g.label]));

// Stato di navigazione per breakpoint: medio = rail (false) / estesa (true); espanso = estesa (true) / rail (false).
const navOpen = { medium: false, expanded: true };
try { if (localStorage.getItem('fotoscan.nav') === 'rail') navOpen.expanded = false; } catch { /* ignora */ }

let size = 'compact';
let shell, outlet, toggle, titleEl, eyebrowEl, searchField, chip, chipProgress, chipText, banner, themeBtn, themeBtnCompact;
let flowBar, flowSteps$ = [], flowNext, sideStatus, sideRefs = null;
let indexState = null;
let facesJob = null;
let dupSummary = null;
let gPending = 0;
// ---------- Stato dei passi del flusso ----------
/** 'done' | 'todo' | 'warn' | 'running' per ciascun passo, calcolato da /status e /duplicates/summary. */
function stepState(id) {
  const st = store.get('status', null);
  const c = st?.counts;
  if (!c) return 'todo';
  if (id === 'index') {
    if (indexState?.running) return 'running';
    return c.totale > 0 && c.indicizzati + (c.errori || 0) >= c.totale ? 'done' : 'todo';
  }
  if (id === 'duplicates') {
    if (!dupSummary) return 'todo';
    return dupSummary.reviewed === true || dupSummary.exact?.groups === 0 ? 'done' : 'warn';
  }
  if (id === 'folders') {
    if (!st.plan?.built) return 'todo';
    return st.plan?.missing > 0 ? 'warn' : 'done';
  }
  return 'todo';
}

const STEP_STATE_LABEL = { done: 'completato', warn: 'da rivedere', running: 'in corso', todo: 'da fare' };
/** Etichetta accessibile dello stato: per le Cartelle "da ricalcolare" è più chiaro di "da rivedere". */
const stateLabel = (id, s) => (s === 'warn' && id === 'folders' ? 'da ricalcolare' : STEP_STATE_LABEL[s]);

function stepMarker(item) {
  const s = stepState(item.id);
  return h('span', { class: `step-marker step-marker--${s}`, 'data-state': s, 'aria-hidden': 'true' },
    s === 'done' ? icon('check') : s === 'running' ? h('md-circular-progress', { indeterminate: true }) : String(item.step));
}

/** Sostituisce il marcatore solo se lo stato è cambiato: così lo spinner non riparte a ogni aggiornamento. */
function setStepMarker(slot, item) {
  if (!slot) return;
  if (slot.firstElementChild?.dataset.state === stepState(item.id)) return;
  slot.replaceChildren(stepMarker(item));
}

// ---------- Sidebar ----------
function navLink(item, { short = false } = {}) {
  const lead = item.step && !short
    ? h('span', { class: 'nav-icon nav-icon--step' }, stepMarker(item))
    : h('span', { class: 'nav-icon' }, icon(item.icon));
  return h('a', {
    class: 'nav-link', href: `#${item.path}`, 'data-id': item.id, title: item.label,
    onpointerenter: () => prefetchRoute(item.path),
    onfocus: () => prefetchRoute(item.path),
  },
  lead,
  h('span', { class: 'nav-label' }, item.label),
  h('md-ripple'));
}

function buildBrand() {
  toggle = h('md-icon-button', {
    class: 'nav-toggle', 'aria-label': 'Comprimi o espandi il menu', title: 'Comprimi o espandi il menu', onclick: toggleNav,
  }, icon('dock_to_right'));
  return h('div', { class: 'nav-brand' },
    h('a', { class: 'brand', href: '#/', 'aria-label': 'PhotoScan, vai alla Home' },
      h('span', { class: 'brand-text' },
        createWordmark({ className: 'brand-wordmark', strokeWidth: 11, variant: 'brand' }),
        h('span', { class: 'brand-sub' }, 'Archivio foto'))),
    toggle);
}

/** Stato archivio in fondo alla sidebar: nodi creati una volta sola e aggiornati sul posto (niente re-render a ogni tick). */
function buildSideStatus() {
  const iconSlot = h('span', { class: 'side-status__icon', 'aria-hidden': 'true' });
  const title = h('span', { class: 'side-status__title' });
  const sub = h('span', { class: 'side-status__sub' });
  const fill = h('span');
  const bar = h('span', { class: 'side-status__bar' }, fill);
  sideStatus = h('a', { class: 'side-status', href: '#/index' },
    iconSlot, h('span', { class: 'side-status__text' }, title, sub, bar));
  sideRefs = { iconSlot, title, sub, bar, fill, iconKey: null };
  renderSideStatus();
  return sideStatus;
}

function renderSideStatus() {
  if (!sideStatus) return;
  const r = sideRefs;
  const c = store.get('status', null)?.counts;
  const ix = indexState;
  const fj = facesJob?.state === 'running' ? facesJob : null;
  let title, sub, frac, tone = '', href = '#/organize?tab=index', ariaTarget = 'Apri la pagina Analisi';
  if (ix?.running) {
    const p = typeof ix.percent === 'number' ? Math.round(ix.percent) : null;
    title = p === null ? 'Analisi in corso' : `Analisi · ${p}%`;
    sub = ix.stage || 'Lettura delle foto…';
    frac = p === null ? null : p / 100;
    tone = 'is-running';
  } else if (fj) {
    const p = fj.total > 0 ? Math.min(100, Math.round((fj.done / fj.total) * 100)) : null;
    title = p === null ? 'Volti in corso' : `Volti · ${p}%`;
    sub = fj.message || 'Riconoscimento volti…';
    frac = p === null ? null : p / 100;
    tone = 'is-running';
    href = '#/people';
    ariaTarget = 'Apri la pagina Persone';
  } else if (c) {
    const errors = c.errori || 0;
    const done = c.totale > 0 && c.indicizzati + errors >= c.totale;
    title = c.totale === 1 ? '1 elemento' : c.totale ? `${formatNumber(c.totale)} elementi` : 'Archivio vuoto';
    sub = done ? (errors ? `Analizzato · ${formatNumber(errors)} illeggibili` : 'Tutto analizzato')
      : c.totale ? `${formatNumber(c.totale - c.indicizzati - errors)} da analizzare` : 'Scegli una cartella';
    frac = c.totale ? (c.indicizzati + errors) / c.totale : 0;
    tone = done ? 'is-done' : 'is-todo';
  } else {
    title = 'Archivio';
    sub = 'Caricamento…';
    frac = 0;
  }
  sideStatus.setAttribute('href', href);
  sideStatus.className = `side-status ${tone}`;
  sideStatus.setAttribute('aria-label', `${title}. ${sub}. ${ariaTarget}`);
  const key = (ix?.running || fj) ? 'spin' : tone === 'is-done' ? 'done' : 'lib';
  if (r.iconKey !== key) {
    r.iconKey = key;
    r.iconSlot.replaceChildren(key === 'spin'
      ? h('span', { class: 'side-status__ring', 'aria-hidden': 'true' })
      : icon(key === 'done' ? 'cloud_done' : 'photo_library'));
  }
  r.title.textContent = title;
  r.sub.textContent = sub;
  r.bar.classList.toggle('is-indeterminate', frac === null);
  r.fill.style.width = `${Math.round((frac ?? 0.3) * 100)}%`;
}

function buildSideNav() {
  const groups = navGroups.filter((g) => g.id !== 'system').map((g) =>
    h('div', { class: `nav-group nav-group--${g.id}` },
      h('p', { class: 'nav-group-label' }, g.label),
      navItems.filter((i) => i.group === g.id).map((i) => navLink(i))));
  themeBtn = h('md-icon-button', {
    class: 'theme-btn nav-theme-btn',
    onclick: () => applyTheme(THEME_NEXT[store.get('theme', 'system')]),
  });
  const sysItems = navItems.filter((i) => i.group === 'system');
  const footerLinks = sysItems.map((i) => (i.id === 'help'
    ? h('div', { class: 'nav-footer-help-row' }, navLink(i), themeBtn)
    : navLink(i)));
  return h('nav', { class: 'app-nav', 'aria-label': 'Navigazione' },
    buildBrand(),
    h('div', { class: 'nav-scroll' }, groups),
    h('div', { class: 'nav-footer' },
      buildSideStatus(),
      ...footerLinks));
}

function refreshStepMarkers() {
  for (const a of shell.querySelectorAll('.app-nav .nav-link')) {
    const item = flowSteps.find((s) => s.id === a.dataset.id);
    if (!item) continue;
    setStepMarker(a.querySelector('.nav-icon--step'), item);
  }
}

// ---------- Top bar ----------
function buildTopbar() {
  eyebrowEl = h('span', { class: 'topbar-eyebrow' });
  titleEl = h('h1', { class: 'topbar-title' }, 'FotoScan');
  const brandCompact = h('img', { class: 'topbar-logo', src: 'favicon.svg', alt: '', width: 28, height: 28 });

  searchField = h('input', {
    class: 'topbar-search-input', type: 'search', placeholder: 'Cerca nelle foto… es. «tramonto», «torta»',
    'aria-label': 'Cerca foto', autocomplete: 'off', spellcheck: false,
  });
  searchField.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') { searchField.value = ''; searchField.blur(); return; }
    if (e.key !== 'Enter') return;
    e.preventDefault();
    const q = searchField.value.trim();
    if (q) navigate('/search', { q });
  });
  const search = h('label', { class: 'topbar-search' },
    icon('search'), searchField, h('span', { class: 'kbd topbar-search-kbd', 'aria-hidden': 'true' }, '/'));

  chipProgress = h('md-circular-progress', { class: 'chip-progress' });
  chipText = h('span', { class: 'status-chip-text' });
  chip = h('button', {
    class: 'status-chip', type: 'button', hidden: true,
    onclick: () => navigate(indexState?.running ? '/index' : '/people'),
  }, chipProgress, chipText);

  themeBtnCompact = h('md-icon-button', {
    class: 'theme-btn menu-compact-only',
    onclick: () => applyTheme(THEME_NEXT[store.get('theme', 'system')]),
  });
  store.subscribe('theme', renderTheme);
  renderTheme(store.get('theme', 'system'));

  const menuItem = (href, ic, label, cls = '') => h('md-menu-item', { href, class: cls },
    icon(ic, { slot: 'start' }), h('div', { slot: 'headline' }, label));
  const menu = h('md-menu', { anchor: 'shell-more-btn', positioning: 'popover' },
    menuItem('#/people', 'face', 'Persone', 'menu-compact-only'),
    menuItem('#/places', 'map', 'Luoghi', 'menu-compact-only'),
    menuItem('#/stats', 'insights', 'Statistiche', 'menu-compact-only'),
    menuItem('#/phone', 'smartphone', 'Telefono', 'menu-compact-only'),
    menuItem('#/settings', 'settings', 'Impostazioni'),
    menuItem('#/help', 'help', 'Aiuto e scorciatoie'));
  const more = h('md-icon-button', {
    id: 'shell-more-btn', 'aria-label': 'Altre opzioni', 'aria-haspopup': 'menu', title: 'Altre opzioni',
    onclick: () => { menu.open = !menu.open; },
  }, icon('more_vert'));

  return h('header', { class: 'app-topbar' },
    h('div', { class: 'topbar-heading' }, brandCompact, h('div', { class: 'topbar-titles' }, eyebrowEl, titleEl)),
    search,
    h('div', { class: 'topbar-actions' }, chip, themeBtnCompact, more, menu));
}

function renderTheme(mode) {
  const m = THEME_ICON[mode] ? mode : 'system';
  const label = `Tema: ${THEME_LABEL[m]}. Cambia tema`;
  for (const btn of [themeBtn, themeBtnCompact]) {
    if (!btn) continue;
    btn.replaceChildren(icon(THEME_ICON[m]));
    btn.setAttribute('aria-label', label);
    btn.setAttribute('title', label);
  }
}

function syncStatusChipVisibility() {
  if (!chip) return;
  const ixRun = !!(indexState && indexState.running);
  const fjRun = !!(facesJob && facesJob.state === 'running');
  const navMode = shell?.dataset.nav || 'bottom';
  const path = currentRoute().path;
  if (ixRun) {
    chip.hidden = path === '/index' || navMode === 'drawer';
  } else if (fjRun) {
    chip.hidden = path === '/people' || navMode === 'drawer';
  } else {
    chip.hidden = true;
  }
}

function renderFacesJob(j) {
  facesJob = j || null;
  store.set('facesJob', facesJob);
  syncStatusChipVisibility();
  renderSideStatus();
  if (indexState?.running || !facesJob || facesJob.state !== 'running') return;
  const p = facesJob.total > 0 ? Math.min(100, Math.round((facesJob.done / facesJob.total) * 100)) : null;
  chipProgress.indeterminate = p === null;
  if (p !== null) chipProgress.value = p / 100;
  chipText.textContent = p === null ? 'Volti' : `Volti ${p}%`;
  chip.setAttribute('aria-label', p === null ? 'Analisi volti in corso. Apri Persone' : `Analisi volti al ${p}%. Apri Persone`);
}

function renderIndex(s) {
  indexState = s || null;
  const running = !!(s && s.running);
  syncStatusChipVisibility();
  renderSideStatus();
  refreshStepMarkers();
  renderFlowBar();
  if (!running) return;
  const p = typeof s.percent === 'number' ? Math.round(s.percent) : null;
  chipProgress.indeterminate = p === null;
  if (p !== null) chipProgress.value = Math.min(1, Math.max(0, p / 100));
  chipText.textContent = p === null ? 'Analisi' : `Analisi ${p}%`;
  chip.setAttribute('aria-label', p === null ? 'Analisi in corso. Apri la pagina Analisi' : `Analisi al ${p}%. Apri la pagina Analisi`);
}

// ---------- Barra del flusso Organizza ----------
function buildFlowBar() {
  flowSteps$ = flowSteps.map((s) => h('a', { class: 'flow-step', href: `#${s.path}`, 'data-id': s.id },
    h('span', { class: 'flow-step__marker' }),
    h('span', { class: 'flow-step__text' },
      h('span', { class: 'flow-step__label' }, s.label),
      h('span', { class: 'flow-step__hint' }, s.hint))));
  flowNext = h('md-filled-tonal-button', { class: 'flow-next', hidden: true });
  flowBar = h('nav', { class: 'flow-bar', 'aria-label': 'Passi per organizzare l’archivio', hidden: true },
    h('ol', { class: 'flow-steps' }, flowSteps$.map((a) => h('li', {}, a))),
    flowNext);
  return flowBar;
}

function renderFlowBar() {
  // Organizza ora è una pagina unica (/organize) con la propria barra dei passi integrata.
  if (flowBar) flowBar.hidden = true;
}

async function loadDupSummary() {
  try {
    dupSummary = await api.get('/duplicates/summary');
    refreshStepMarkers();
    renderFlowBar();
  } catch { /* facoltativo */ }
}
// La pagina Duplicati segnala con questo evento che l'utente ha revisionato i gruppi.
window.addEventListener('fotoscan:dup-reviewed', () => loadDupSummary());

// ---------- Layout ----------
function buildBanner() {
  return h('div', { class: 'app-banner', role: 'alert', hidden: true },
    icon('cloud_off'),
    h('span', {}, 'Connessione al server persa. Riprovo automaticamente…'));
}

function applyLayout() {
  size = MQ_EXPANDED.matches ? 'expanded' : MQ_MEDIUM.matches ? 'medium' : 'compact';
  const mode = size === 'compact' ? 'bottom' : navOpen[size] ? 'drawer' : 'rail';
  shell.dataset.size = size;
  shell.dataset.nav = mode;
  toggle.setAttribute('aria-expanded', String(mode === 'drawer'));
  const ic = mode === 'drawer' ? 'menu_open' : 'menu';
  if (toggle.dataset.ic !== ic) {
    toggle.dataset.ic = ic;
    toggle.replaceChildren(icon(ic));
  }
  syncStatusChipVisibility();
  renderFlowBar(); // la misura della lista dipende dalla larghezza della colonna
}

/** Comprimi/espandi: il contenuto della sidebar sfuma, cambia layout mentre la colonna è ferma, poi ricompare. */
async function toggleNav() {
  navOpen[size] = !navOpen[size];
  if (size === 'expanded') {
    try { localStorage.setItem('fotoscan.nav', navOpen.expanded ? 'drawer' : 'rail'); } catch { /* ignora */ }
  }
  const nav = shell.querySelector('.app-nav');
  if (REDUCED.matches || !nav?.animate) return applyLayout();
  const out = nav.animate([{ opacity: 1 }, { opacity: 0 }], { duration: 100, easing: 'linear', fill: 'forwards' });
  await out.finished.catch(() => {});
  applyLayout();
  out.cancel();
  nav.animate([{ opacity: 0 }, { opacity: 1 }], {
    duration: parseFloat(tokenValue('--dur-medium')) || 0,
    delay: 80,
    easing: tokenValue('--motion-standard'),
    fill: 'backwards',
  });
}

function setActive(path) {
  const id = activeNavId(path);
  const flow = isFlowPath(path);
  for (const a of shell.querySelectorAll('.nav-link[data-id]')) {
    const on = a.dataset.id === id || (a.dataset.id === organizeEntry.id && flow);
    a.classList.toggle('is-active', on);
    if (on) a.setAttribute('aria-current', 'page'); else a.removeAttribute('aria-current');
  }
}

function isTyping(t) {
  return !!t && (t.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName));
}

function bindShortcuts() {
  const dest = { h: '/', f: '/gallery', s: '/search', a: '/albums', o: '/organize' };
  addEventListener('keydown', (e) => {
    if (e.ctrlKey || e.metaKey || e.altKey) return;
    if (isTyping(e.composedPath()[0])) return;
    const k = e.key.toLowerCase();
    if (e.key === '/') {
      e.preventDefault();
      if (shell.dataset.route === 'search') {
        outlet.querySelector('md-outlined-text-field, input')?.focus();
      } else if (searchField && searchField.offsetParent !== null) {
        searchField.focus();
      } else {
        navigate('/search');
      }
      return;
    }
    if (e.key === '?') { e.preventDefault(); navigate('/help'); return; }
    if (gPending && Date.now() - gPending < 1200) {
      gPending = 0;
      if (dest[k]) { e.preventDefault(); navigate(dest[k]); }
      return;
    }
    gPending = k === 'g' ? Date.now() : 0;
  });
}

/** Valore di un token di movimento (WAAPI non accetta var(), quindi lo leggiamo dal CSS). */
const tokenValue = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();

/** Monta la shell dentro appEl e ritorna l'elemento .view-outlet dove il router monta le view. */
export async function mountShell(appEl) {
  outlet = h('main', { class: 'view-outlet', id: 'main', tabindex: '-1' });
  const skip = h('a', {
    class: 'skip-link', href: '#main',
    onclick: (e) => { e.preventDefault(); outlet.focus(); },
  }, 'Vai al contenuto');
  banner = buildBanner();
  const sideNav = buildSideNav();
  const bottomItems = [...navItems.filter((i) => i.primary).slice(0, 4), organizeEntry];
  const bottomNav = h('nav', { class: 'app-bottombar', 'aria-label': 'Navigazione principale' },
    bottomItems.map((i) => navLink(i, { short: true })));

  shell = h('div', { class: 'app-shell', 'data-size': 'compact', 'data-nav': 'bottom' },
    skip,
    sideNav,
    h('div', { class: 'app-main' }, buildTopbar(), banner, buildFlowBar(), outlet),
    bottomNav);
  appEl.replaceChildren(shell);

  bindShortcuts();
  MQ_MEDIUM.addEventListener('change', applyLayout);
  MQ_EXPANDED.addEventListener('change', applyLayout);
  applyLayout();

  // Stato indicizzazione e volti: stato iniziale + aggiornamenti SSE cross-pagina.
  api.get('/index/status').then(renderIndex).catch(() => { /* il banner di connessione segnala i problemi */ });
  api.get('/people/job').then((r) => renderFacesJob(r?.job || null)).catch(() => {});
  onEvent('index', (s) => {
    const was = indexState?.running;
    renderIndex(s);
    if (was && !s?.running) api.get('/status').then((st) => store.set('status', st)).catch(() => {});
  });
  onEvent('job', (ev) => {
    if (!ev || !String(ev.name || '').startsWith('volti_')) return;
    const was = facesJob?.state === 'running';
    renderFacesJob(ev);
    if (was && ev.state !== 'running') {
      api.get('/status').then((st) => store.set('status', st)).catch(() => {});
    }
  });
  store.subscribe('status', () => { renderSideStatus(); refreshStepMarkers(); renderFlowBar(); loadDupSummary(); });
  loadDupSummary();

  // Connessione: banner quando il server non risponde; al ritorno ricarica lo stato.
  onEvent('connection', ({ online }) => {
    banner.hidden = !!online;
    if (online) {
      api.get('/index/status').then(renderIndex).catch(() => {});
      api.get('/people/job').then((r) => renderFacesJob(r?.job || null)).catch(() => {});
    }
  });

  // Titolo, voce attiva, ricerca pre-compilata, barra del flusso, transizione leggera fra le pagine.
  let prevPath = null;
  let wasScrolled = false;
  onRoute((cur) => {
    const id = activeNavId(cur.path);
    const item = navItems.find((i) => i.id === id);
    titleEl.textContent = cur.route ? cur.route.title : 'FotoScan';
    eyebrowEl.textContent = cur.path === '/' ? '' : item ? (item.step ? `Organizza · Passo ${item.step} di ${flowSteps.length}` : GROUP_LABEL[item.group] || '') : '';
    shell.dataset.route = cur.path.split('/')[1] || 'home';
    setActive(cur.path);
    if (isFlowPath(cur.path)) loadDupSummary(); // lo stato dei duplicati può essere cambiato in un'altra pagina
    syncStatusChipVisibility();
    renderFlowBar();
    if (cur.path === '/search') searchField.value = cur.query.q || '';
    else if (prevPath === '/search') searchField.value = '';
    prevPath = cur.path;
    wasScrolled = false;
    shell.classList.remove('is-scrolled');
  });
  setActive(currentRoute().path);

  // Ombra sotto la top bar quando il contenuto scorre (aggiorna il DOM solo quando lo stato cambia).
  outlet.addEventListener('scroll', () => {
    const scrolled = outlet.scrollTop > 4;
    if (scrolled !== wasScrolled) {
      wasScrolled = scrolled;
      shell.classList.toggle('is-scrolled', scrolled);
    }
  }, { passive: true });

  // Se la view ha un proprio h1 la top bar non ripete il titolo (data-view-title nasconde il titolo della shell).
  // Ignora le mutazioni dentro griglie virtuali/foto per evitare scansioni del DOM a ogni frame di scroll.
  let titleRaf = 0;
  const syncViewTitle = () => {
    titleRaf = 0;
    shell.toggleAttribute('data-view-title', !!outlet.querySelector('h1:not(.sr-only)'));
  };
  new MutationObserver((mutations) => {
    if (titleRaf) return;
    for (const m of mutations) {
      if (m.target.closest?.('.vg-layer, .tile-grid, .gallery-scroll')) continue;
      titleRaf = requestAnimationFrame(syncViewTitle);
      return;
    }
  }).observe(outlet, { childList: true, subtree: true });

  return outlet;
}
