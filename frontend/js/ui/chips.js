// Chip filtro a selezione singola (comportamento da radio) su md-chip-set / md-filter-chip.
import { icon } from './el.js';

/**
 * filterChips([{id,label,icon?}], {selected, allowNone=false, label='Filtri', onChange(id)}) -> HTMLElement
 * - Scegliere un chip deseleziona gli altri. Con allowNone, un secondo tocco sul chip attivo lo deseleziona (id = null).
 * - Il nodo ritornato ha il metodo setSelected(id): aggiorna lo stato SENZA chiamare onChange.
 * - Frecce sinistra/destra spostano il fuoco e selezionano (come un gruppo di radio).
 */
export function filterChips(items, { selected = null, allowNone = false, label = 'Filtri', onChange } = {}) {
  const set = document.createElement('md-chip-set');
  set.setAttribute('role', 'radiogroup');
  set.setAttribute('aria-label', label);

  let current = selected ?? null;
  const chips = new Map();

  const paint = () => {
    for (const [id, chip] of chips) {
      const on = id === current;
      chip.selected = on;
      chip.setAttribute('aria-checked', String(on));
    }
  };

  for (const item of items) {
    const chip = document.createElement('md-filter-chip');
    chip.setAttribute('role', 'radio');
    chip.setAttribute('label', item.label);
    if (item.icon) {
      const ic = icon(item.icon, { slot: 'icon' });
      chip.append(ic);
    }
    chip.addEventListener('click', () => {
      // md-filter-chip alterna da sola il proprio stato: lo riallineiamo al modello a selezione singola.
      const prev = current;
      if (item.id === current) {
        if (allowNone) current = null;
      } else {
        current = item.id;
      }
      paint();
      if (current !== prev) onChange?.(current);
    });
    chips.set(item.id, chip);
    set.append(chip);
  }

  set.addEventListener('keydown', (e) => {
    if (e.key !== 'ArrowRight' && e.key !== 'ArrowLeft') return;
    const list = [...chips.values()];
    const idx = list.indexOf(e.target); // il target e' l'host md-filter-chip (light DOM)
    if (idx < 0) return;
    e.preventDefault();
    const next = list[(idx + (e.key === 'ArrowRight' ? 1 : list.length - 1)) % list.length];
    next.focus();
    next.click(); // il listener sull'host applica la selezione singola
  });

  set.setSelected = (id) => {
    current = id ?? null;
    paint();
  };

  paint();
  return set;
}
