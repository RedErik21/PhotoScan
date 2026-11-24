// Selezione multipla osservabile, barra contestuale M3 e helper Shift-click.
import { h, icon } from './el.js';

const VISIBLE_ACTIONS = 4; // oltre la quarta azione si va nel menu "Altre azioni"
const TRANSITION_MS = 250; // = --dur-medium

const sameSet = (a, b) => a.size === b.size && [...a].every((x) => b.has(x));

/**
 * createSelection() -> { ids:Set, count, has, toggle, add, remove, clear, selectAll, selectRange, subscribe }
 * subscribe(fn) chiama fn(selection) a ogni variazione reale e ritorna la funzione di annullamento.
 */
export function createSelection() {
  const ids = new Set();
  const subs = new Set();
  const emit = () => subs.forEach((fn) => fn(api));

  const api = {
    ids,
    get count() {
      return ids.size;
    },
    has: (id) => ids.has(id),
    toggle(id) {
      if (ids.has(id)) ids.delete(id);
      else ids.add(id);
      emit();
      return ids.has(id);
    },
    add(id) {
      if (!ids.has(id)) {
        ids.add(id);
        emit();
      }
    },
    remove(id) {
      if (ids.delete(id)) emit();
    },
    clear() {
      if (ids.size) {
        ids.clear();
        emit();
      }
    },
    /** Sostituisce la selezione con l'elenco dato. */
    selectAll(list) {
      const next = new Set(list);
      if (!sameSet(next, ids)) {
        ids.clear();
        next.forEach((id) => ids.add(id));
        emit();
      }
    },
    /** Aggiunge alla selezione tutti gli id tra fromId e toId (estremi inclusi) dell'elenco ordinato. */
    selectRange(idsInOrder, fromId, toId) {
      const a = idsInOrder.indexOf(fromId);
      const b = idsInOrder.indexOf(toId);
      if (b < 0) return;
      if (a < 0) return api.add(toId);
      const [lo, hi] = a < b ? [a, b] : [b, a];
      let changed = false;
      for (let i = lo; i <= hi; i++) {
        if (!ids.has(idsInOrder[i])) {
          ids.add(idsInOrder[i]);
          changed = true;
        }
      }
      if (changed) emit();
    },
    subscribe(fn) {
      subs.add(fn);
      return () => subs.delete(fn);
    },
  };
  return api;
}

/**
 * useRangeSelect(selection, getOrderedIds) -> onItemClick(id, event)
 * Click normale: alterna l'elemento e lo imposta come ancora. Shift+click: seleziona l'intervallo
 * dall'ancora fino a id (l'ancora resta invariata). event e' l'evento DOM (solo shiftKey viene letto).
 */
export function useRangeSelect(selection, getOrderedIds) {
  let anchor = null;
  const onItemClick = (id, event = {}) => {
    const ordered = getOrderedIds();
    if (event.shiftKey && anchor !== null && ordered.includes(anchor)) {
      event.preventDefault?.(); // evita la selezione di testo della griglia
      selection.selectRange(ordered, anchor, id);
    } else {
      selection.toggle(id);
    }
    anchor = id;
  };
  onItemClick.reset = () => {
    anchor = null;
  };
  return onItemClick;
}

let uid = 0;

/**
 * selectionBar(host, {selection, actions:[{id,icon,label,run(ids)}]}) -> {destroy()}
 * Barra contestuale: compare quando count>0, chiude con il pulsante "Annulla selezione" o con Esc.
 * Stili in css/components.css (.selection-bar).
 */
export function selectionBar(host, { selection, actions = [] } = {}) {
  const n = ++uid;
  const runAction = (a) => a.run(Array.from(selection.ids));

  const countEl = h('span', { class: 'selection-bar__count md-typescale-title-medium', 'aria-live': 'polite' });
  const closeBtn = h(
    'md-icon-button',
    { 'aria-label': 'Annulla selezione', title: 'Annulla selezione', onclick: () => selection.clear() },
    icon('close'),
  );

  const actionsEl = h('div', { class: 'selection-bar__actions' });
  // `label` = nome accessibile e tooltip. `text` (facoltativo) = testo visibile accanto all'icona da 600px;
  // sotto 600px resta solo l'icona. Senza `text` l'azione e' sempre solo icona.
  for (const a of actions.slice(0, VISIBLE_ACTIONS)) {
    actionsEl.append(a.text
      ? h('md-text-button', { 'aria-label': a.label, title: a.label, onclick: () => runAction(a) },
        icon(a.icon, { slot: 'icon' }),
        h('span', { class: 'selection-bar__label' }, a.text))
      : h('md-icon-button', { 'aria-label': a.label, title: a.label, onclick: () => runAction(a) }, icon(a.icon)));
  }

  let menu = null;
  const overflow = actions.slice(VISIBLE_ACTIONS);
  if (overflow.length) {
    const anchorId = `selection-bar-more-${n}`;
    actionsEl.append(
      h(
        'md-icon-button',
        {
          id: anchorId,
          'aria-label': 'Altre azioni',
          title: 'Altre azioni',
          'aria-haspopup': 'menu',
          onclick: () => (menu.open = !menu.open), // md-menu non si apre da solo dall'ancora
        },
        icon('more_vert'),
      ),
    );
    menu = h(
      'md-menu',
      { anchor: anchorId, positioning: 'popover' },
      ...overflow.map((a) =>
        h(
          'md-menu-item',
          { onclick: () => runAction(a) },
          icon(a.icon, { slot: 'start' }),
          h('div', { slot: 'headline' }, a.label),
        ),
      ),
    );
    actionsEl.append(menu);
  }

  const bar = h(
    'div',
    { class: 'selection-bar', role: 'toolbar', 'aria-label': 'Azioni sulla selezione', hidden: true },
    closeBtn,
    countEl,
    actionsEl,
  );
  // Lo slot (altezza 0) tiene la barra in overlay in cima alla vista: la griglia sotto non si sposta.
  const slot = h('div', { class: 'selection-bar-slot' }, bar);
  host.prepend(slot);

  let hideTimer = null;
  const reduceMotion = window.matchMedia?.('(prefers-reduced-motion: reduce)');
  const show = () => {
    clearTimeout(hideTimer);
    bar.hidden = false;
    void bar.offsetWidth; // forza il layout prima della transizione di ingresso
    bar.classList.add('is-open');
  };
  const hide = () => {
    bar.classList.remove('is-open');
    clearTimeout(hideTimer);
    hideTimer = setTimeout(() => (bar.hidden = true), reduceMotion?.matches ? 0 : TRANSITION_MS);
  };

  const render = () => {
    const c = selection.count;
    countEl.textContent = c === 1 ? '1 selezionata' : `${c} selezionate`;
    if (c > 0) show();
    else hide();
  };

  const onKey = (e) => {
    if (e.key !== 'Escape' || e.defaultPrevented || selection.count === 0) return;
    if (menu?.open || document.querySelector('md-dialog[open]')) return; // Esc chiude prima il menu/dialogo
    selection.clear();
  };
  document.addEventListener('keydown', onKey);
  const unsubscribe = selection.subscribe(render);
  render();

  return {
    destroy() {
      unsubscribe();
      document.removeEventListener('keydown', onKey);
      clearTimeout(hideTimer);
      slot.remove();
    },
  };
}
