// Stati vuoti, di errore e di attesa breve. Stili in css/components.css (.empty-state*, .loading-block*).
import { h, icon as materialIcon } from './el.js';

/** Icona puramente decorativa (il testo dello stato dice già tutto). */
function decorativeIcon(name) {
  const ic = materialIcon(name);
  ic.setAttribute('aria-hidden', 'true');
  ic.classList.add('empty-state__icon');
  return ic;
}

/** Stato vuoto: icona, titolo, testo facoltativo e azione facoltativa. */
export function emptyState({ icon: name = 'photo_library', title, message, actionLabel, onAction } = {}) {
  return h('div', { class: 'empty-state', role: 'status' },
    decorativeIcon(name),
    h('h2', { class: 'empty-state__title' }, title),
    message ? h('p', { class: 'empty-state__text' }, message) : null,
    actionLabel ? h('md-filled-tonal-button', { class: 'empty-state__action', onclick: onAction }, actionLabel) : null);
}

/** Stato di errore con pulsante "Riprova" (se è fornito onRetry). Annunciato subito come alert. */
export function errorState({ title = 'Qualcosa non ha funzionato', message, onRetry } = {}) {
  return h('div', { class: 'empty-state empty-state--error', role: 'alert' },
    decorativeIcon('error'),
    h('h2', { class: 'empty-state__title' }, title),
    message ? h('p', { class: 'empty-state__text' }, message) : null,
    onRetry ? h('md-filled-tonal-button', { class: 'empty-state__action', onclick: onRetry }, 'Riprova') : null);
}

/** Attesa breve (dialog, sezioni): spinner indeterminato M3 + testo. */
export function loadingBlock(label = 'Caricamento in corso…') {
  return h('div', { class: 'loading-block', role: 'status' },
    h('md-circular-progress', { indeterminate: true, 'aria-hidden': 'true' }),
    h('p', { class: 'loading-block__label' }, label));
}
