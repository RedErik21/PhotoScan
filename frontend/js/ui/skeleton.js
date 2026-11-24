// Segnaposto di caricamento (skeleton). Le classi visive stanno in css/components.css.
// Il contenitore ha aria-busy="true"; i segnaposto sono nascosti agli screen reader.
import { h } from './el.js';

// Il contenitore annuncia il caricamento (role=status + nome): altrimenti gli screen reader non dicono nulla.
const busy = (props) => ({ 'aria-busy': 'true', role: 'status', 'aria-label': 'Caricamento in corso', ...props });
const hidden = { 'aria-hidden': 'true' };

/** Griglia di riquadri quadrati, come le miniature della galleria. */
export function skeletonGrid(count = 24, { tile = 180 } = {}) {
  return h('div', busy({
    class: 'skeleton-grid',
    style: `--skeleton-tile:${tile}px`,
  }), Array.from({ length: count }, () => h('div', { class: 'skeleton skeleton-tile', ...hidden })));
}

/** Elenco di righe: cerchio + due linee di testo. */
export function skeletonList(count = 6) {
  return h('div', busy({ class: 'skeleton-list' }),
    Array.from({ length: count }, (_, i) => h('div', { class: 'skeleton-row', ...hidden },
      h('div', { class: 'skeleton skeleton-avatar' }),
      h('div', { class: 'skeleton-row-text' },
        h('div', { class: 'skeleton skeleton-line', style: 'width:60%' }),
        h('div', { class: 'skeleton skeleton-line skeleton-line--short', style: `width:${40 + ((i * 17) % 35)}%` })))));
}

/** Scheda: blocco immagine + tre linee di testo. */
export function skeletonCard() {
  return h('div', busy({ class: 'skeleton-card card' }),
    h('div', { class: 'skeleton skeleton-card-media', ...hidden }),
    h('div', { class: 'skeleton-card-body', ...hidden },
      h('div', { class: 'skeleton skeleton-line', style: 'width:70%' }),
      h('div', { class: 'skeleton skeleton-line', style: 'width:50%' }),
      h('div', { class: 'skeleton skeleton-line skeleton-line--short', style: 'width:30%' })));
}
