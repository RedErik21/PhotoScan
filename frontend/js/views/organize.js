// Pagina unica "Organizza": unifica Analisi, Duplicati, Cartelle proposte e Crea cartelle
// in un solo spazio di lavoro con barra di stato live e cambio scheda istantaneo.
import { h, icon } from '../ui/el.js';
import { api, onEvent } from '../api.js';
import { store } from '../store.js';
import { formatNumber, plural } from '../ui/format.js';
import { skeletonCard } from '../ui/skeleton.js';
import { errorState } from '../ui/empty-state.js';

const TABS = [
  { id: 'index',   step: 1, label: 'Analisi',       file: './indexing.js' },
  { id: 'cleanup', step: 2, label: 'Libera spazio', file: './cleanup.js' },
];

const PATH_TO_TAB = {
  '/index': 'index',
  '/duplicates': 'cleanup',
  '/cleanup': 'cleanup',
};

const CSS_URL = new URL('../../css/view-organize.css', import.meta.url).href;
function loadCss() {
  if (document.querySelector('link[data-view-css="organize"]')) return;
  document.head.append(h('link', { rel: 'stylesheet', href: CSS_URL, dataset: { viewCss: 'organize' } }));
}

export async function mount(root, ctx) {
  loadCss();
  let alive = true;
  let seq = 0;
  let activeId = PATH_TO_TAB[ctx.path] || (TABS.some((t) => t.id === ctx.query?.tab) ? ctx.query.tab : null);
  let cleanupSub = null;
  let subAc = null;

  let status = store.get('status', null);
  let indexState = null;
  let dupSummary = null;

  const nextBtn = h('md-filled-tonal-button', { hidden: true });
  const header = h('header', { class: 'view-header' },
    h('div', {},
      h('h1', { class: 't-headline' }, 'Organizza'),
      h('p', { class: 'muted t-body' }, 'Analizza l’archivio e libera spazio da duplicati, video pesanti e screenshot inutili.')),
    h('div', { class: 'view-header-actions' }, nextBtn));

  const tabRefs = new Map();
  const stepsList = h('ol', { class: 'org-steps', role: 'tablist', 'aria-label': 'Passi per organizzare' },
    TABS.map((t) => {
      const badge = h('span', { class: 'org-tab__badge', 'aria-hidden': 'true' }, String(t.step));
      const meta = h('span', { class: 'org-tab__meta' }, '—');
      const btn = h('button', {
        type: 'button', role: 'tab', class: 'org-tab', 'data-id': t.id,
        onclick: () => switchTab(t.id),
      },
      badge,
      h('span', { class: 'org-tab__copy' },
        h('span', { class: 'org-tab__title' }, t.label),
        meta));
      tabRefs.set(t.id, { btn, badge, meta, tab: t });
      return h('li', {}, btn);
    }));

  const bodyHost = h('div', { class: 'org-body', role: 'tabpanel' });
  const view = h('div', { class: 'view org-view' }, header, stepsList, bodyHost);
  root.replaceChildren(view);

  function computeStepInfo(id) {
    const c = status?.counts;
    const plan = status?.plan;
    if (id === 'index') {
      if (indexState?.running) {
        const p = typeof indexState.percent === 'number' ? `${Math.round(indexState.percent)}%` : 'in corso…';
        return { state: 'running', text: `Analisi ${p}` };
      }
      if (!c) return { state: 'todo', text: 'Controllo stato…' };
      const done = c.totale > 0 && c.indicizzati + (c.errori || 0) >= c.totale;
      return done
        ? { state: 'done', text: `${formatNumber(c.indicizzati)} file pronti` }
        : { state: 'todo', text: `${formatNumber(c.indicizzati)} di ${formatNumber(c.totale)}` };
    }
    if (id === 'cleanup') {
      if (!dupSummary) return { state: 'todo', text: 'Verifica spazio…' };
      const groups = (dupSummary.exact?.groups || 0) + (dupSummary.similar?.groups || 0);
      if (groups === 0) return { state: 'done', text: 'Duplicati ok · controlla file pesanti' };
      if (dupSummary.reviewed) return { state: 'done', text: `${plural(groups, 'gruppo rivisto', 'gruppi rivisti')}` };
      return { state: 'warn', text: `${plural(groups, 'gruppo duplicato', 'gruppi duplicati')} e altro` };
    }
    if (id === 'folders') {
      if (!plan) return { state: 'todo', text: '—' };
      if (!plan.built) return { state: 'todo', text: 'Da calcolare' };
      if (plan.missing > 0) return { state: 'warn', text: `${formatNumber(plan.missing)} nuove da includere` };
      return { state: 'done', text: plural(plan.folders, 'cartella', 'cartelle') };
    }
    return { state: 'todo', text: 'Applica su disco' };
  }

  function renderTabs() {
    for (const t of TABS) {
      const ref = tabRefs.get(t.id);
      const info = computeStepInfo(t.id);
      const active = t.id === activeId;
      ref.btn.classList.toggle('is-active', active);
      ref.btn.setAttribute('aria-selected', String(active));
      ref.btn.dataset.state = info.state;
      ref.meta.textContent = info.text;
      if (ref.badge.dataset.state !== info.state) {
        ref.badge.dataset.state = info.state;
        ref.badge.replaceChildren(
          info.state === 'done' ? icon('check')
            : info.state === 'running' ? h('span', { class: 'org-tab__ring', 'aria-hidden': 'true' })
              : String(t.step));
      }
    }
    const idx = TABS.findIndex((t) => t.id === activeId);
    const next = TABS[idx + 1];
    nextBtn.hidden = !next;
    if (next) {
      nextBtn.replaceChildren(`Avanti: ${next.label}`, icon('arrow_forward', { slot: 'icon' }));
      nextBtn.setAttribute('trailing-icon', '');
      nextBtn.onclick = () => switchTab(next.id);
    }
  }

  function pickDefaultTab() {
    for (const id of ['index', 'cleanup']) {
      if (computeStepInfo(id).state !== 'done') return id;
    }
    return 'cleanup';
  }

  async function mountSub(id) {
    const my = ++seq;
    if (subAc) { subAc.abort(); subAc = null; }
    if (cleanupSub) {
      try { cleanupSub(); } catch (e) { console.error(e); }
      cleanupSub = null;
    }
    const tab = TABS.find((t) => t.id === id) || TABS[0];
    activeId = tab.id;
    renderTabs();
    bodyHost.replaceChildren(skeletonCard());
    // Re-trigger ingresso leggero del pannello
    bodyHost.style.animation = 'none';
    void bodyHost.offsetWidth;
    bodyHost.style.animation = '';

    subAc = new AbortController();
    const myAc = subAc;
    try {
      const mod = await import(tab.file);
      if (!alive || my !== seq) return;
      bodyHost.replaceChildren();
      const subNavigate = (path, query) => {
        const mapped = PATH_TO_TAB[path];
        if (mapped) { switchTab(mapped, query); return; }
        ctx.navigate(path, query);
      };
      const res = await mod.mount(bodyHost, {
        ...ctx,
        path: `/${tab.id}`,
        query: ctx.query || {},
        signal: myAc.signal,
        navigate: subNavigate,
      });
      if (!alive || my !== seq) { if (typeof res === 'function') res(); return; }
      if (typeof res === 'function') cleanupSub = res;
    } catch (e) {
      if (!alive || my !== seq) return;
      bodyHost.replaceChildren(errorState({ title: 'Sezione non disponibile', message: e.message, onRetry: () => mountSub(id) }));
    }
  }

  function switchTab(id, extraQuery = null) {
    if (!alive) return;
    const params = new URLSearchParams(extraQuery || {});
    params.set('tab', id);
    const hash = `#/organize?${params.toString()}`;
    if (location.hash !== hash) history.replaceState(null, '', hash);
    if (id !== activeId) mountSub(id);
  }

  async function loadMeta() {
    const [st, ix, dup] = await Promise.allSettled([
      api.get('/status', undefined, ctx.signal),
      api.get('/index/status', undefined, ctx.signal),
      api.get('/duplicates/summary', undefined, ctx.signal),
    ]);
    if (!alive) return;
    if (st.status === 'fulfilled') { status = st.value; store.set('status', st.value); }
    if (ix.status === 'fulfilled') indexState = ix.value;
    if (dup.status === 'fulfilled') dupSummary = dup.value;
    if (!activeId) activeId = pickDefaultTab();
    renderTabs();
  }

  const offIndex = onEvent('index', (s) => {
    if (!alive) return;
    const was = indexState?.running;
    indexState = s;
    renderTabs();
    if (was && !s?.running) loadMeta();
  });
  const offStatus = store.subscribe('status', (st) => {
    if (!alive || !st) return;
    status = st;
    renderTabs();
  });
  const onDupReviewed = () => {
    api.get('/duplicates/summary').then((d) => { if (alive) { dupSummary = d; renderTabs(); } }).catch(() => {});
  };
  window.addEventListener('fotoscan:dup-reviewed', onDupReviewed);

  await loadMeta();
  if (alive) await mountSub(activeId || 'index');

  return () => {
    alive = false;
    seq++;
    if (subAc) subAc.abort();
    if (cleanupSub) { try { cleanupSub(); } catch (e) { console.error(e); } }
    offIndex();
    offStatus();
    window.removeEventListener('fotoscan:dup-reviewed', onDupReviewed);
  };
}
