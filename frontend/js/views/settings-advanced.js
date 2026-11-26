// Impostazioni > Avanzate (per esperti): opzioni che cambiano il comportamento o la velocita' di elaborazione.
// Predefiniti sicuri: lasciando tutto com'e' il risultato non cambia. Dati da GET /api/settings, salvataggi immediati
// con PUT /api/settings (un campo alla volta), indipendenti dalla barra «Salva» della pagina.
import { h, icon } from '../ui/el.js';
import { api } from '../api.js';
import { skeletonList } from '../ui/skeleton.js';
import { showSnackbar } from '../ui/snackbar.js';
import { confirmDialog } from '../ui/dialog.js';

const DEFAULTS = { faces_cluster_mode: 'exact', faces_topk: 50, apply_verify: 'full' };

const STYLE_ID = 'fs-settings-advanced-style';
const CSS = `
.settings-group { display: flex; flex-direction: column; gap: var(--sp-3); min-width: 0; }
.settings-group__title { margin: 0; color: var(--md-sys-color-on-surface); }
.settings-actions { display: flex; justify-content: flex-end; }
`;
function injectStyle() {
  if (document.getElementById(STYLE_ID)) return;
  const st = document.createElement('style');
  st.id = STYLE_ID;
  st.textContent = CSS;
  document.head.append(st);
}

const callout = (kind, ic, title, ...body) => h('div', { class: `callout callout--${kind}`, role: 'status' },
  h('span', { class: 'callout__icon material-symbols-outlined', 'aria-hidden': 'true' }, ic),
  h('div', {}, h('p', { class: 'callout__title' }, title), ...body));
const hint = (text) => h('p', { class: 'settings-hint md-typescale-body-medium' }, text);
const errText = (e) => String(e?.message || e || '').replace(/^\w+Error: /, '');

/** Contenitore della scheda Avanzate (settings.js: mountSection(container, ctx) -> {destroy}). */
export async function mountSection(container) {
  injectStyle();
  let disposed = false;
  container.replaceChildren(skeletonList(3));
  let current = null;

  async function load() {
    try {
      current = await api.get('/settings');
      if (!disposed) render();
    } catch (e) {
      if (!disposed) container.replaceChildren(callout('error', 'error', 'Impossibile leggere le impostazioni', hint(errText(e))));
    }
  }

  /** Salva un campo subito. Ritorna true se il server lo ha accettato. */
  async function save(key, value) {
    try {
      current = await api.put('/settings', { [key]: value });
      showSnackbar('Impostazione salvata.');
      return true;
    } catch (e) {
      showSnackbar(errText(e), { error: true });
      return false;
    } finally {
      if (!disposed) render();
    }
  }

  function select(key, label, options) {
    const sel = h('md-outlined-select', { label, 'aria-label': label, value: String(current[key]) },
      ...options.map(([v, t]) => h('md-select-option', { value: v, selected: String(current[key]) === v || null },
        h('div', { slot: 'headline' }, t))));
    sel.addEventListener('change', () => save(key, sel.value));
    return sel;
  }

  function topkField() {
    const tf = h('md-outlined-text-field', { label: 'Vicini per volto', type: 'number', min: '5', max: '500', step: '1', value: String(current.faces_topk) });
    tf.addEventListener('change', () => {
      const n = Number(tf.value);
      if (!Number.isInteger(n) || n < 5 || n > 500) {
        showSnackbar('Inserisci un numero intero tra 5 e 500.', { error: true });
        tf.value = String(current.faces_topk);
        return;
      }
      save('faces_topk', n);
    });
    return tf;
  }

  function render() {
    const isApprox = current.faces_cluster_mode === 'approx';
    const reset = h('md-outlined-button', {
      onclick: async () => {
        const ok = await confirmDialog({ title: 'Ripristina predefiniti', message: 'Riporta le opzioni avanzate ai valori sicuri?', confirmLabel: 'Ripristina' });
        if (!ok) return;
        for (const [k, v] of Object.entries(DEFAULTS)) {
          try { current = await api.put('/settings', { [k]: v }); } catch (e) { showSnackbar(errText(e), { error: true }); return; }
        }
        showSnackbar('Predefiniti ripristinati.');
        render();
      },
    }, icon('restart_alt', { slot: 'icon' }), 'Ripristina predefiniti');

    container.replaceChildren(
      callout('warning', 'warning', 'Sezione per esperti',
        hint('Queste opzioni cambiano il comportamento dell\'app. Con i valori predefiniti il risultato resta quello standard: '
          + 'modificale solo se sai cosa stai facendo. Ogni modifica si salva subito.')),

      h('section', { class: 'settings-group' },
        h('h3', { class: 'settings-group__title md-typescale-title-medium' }, 'Raggruppamento volti'),
        select('faces_cluster_mode', 'Modalità di raggruppamento', [
          ['exact', 'Esatta — predefinita: confronta tutte le coppie di volti'],
          ['approx', 'Approssimata — più veloce su archivi grandi, può separare volti della stessa persona'],
        ]),
        isApprox ? topkField() : null,
        hint(isApprox
          ? 'Ogni volto viene confrontato solo con i suoi vicini più simili. Dopo il cambio, rilancia il riconoscimento volti: '
            + 'le persone già create non vengono ricalcolate da sole.'
          : 'La modalità esatta non perde nessun abbinamento. Usa l\'approssimata solo se il riconoscimento è troppo lento.')),

      h('section', { class: 'settings-group' },
        h('h3', { class: 'settings-group__title md-typescale-title-medium' }, 'Riapplicazione del piano'),
        select('apply_verify', 'Verifica dei file già presenti', [
          ['full', 'Completa — predefinita: verifica il contenuto (hash) dei file già copiati'],
          ['fast', 'Rapida — controlla solo la dimensione: non rilegge l\'archivio, ma non rileva un file danneggiato di pari dimensione'],
        ]),
        hint('La verifica completa rilegge tutto l\'archivio di destinazione a ogni riapplicazione. La rapida è più veloce ma '
          + 'non garantisce che i file in destinazione siano integri.')),

      h('section', { class: 'settings-group' },
        h('h3', { class: 'settings-group__title md-typescale-title-medium' }, 'Metadati Google Takeout'),
        hint('I file JSON accanto alle foto vengono riletti a ogni scansione: i nuovi sidecar sono trovati sempre. '
          + 'Non ci sono opzioni da scegliere.')),

      h('div', { class: 'settings-actions' }, reset),
    );
  }

  load();
  return { destroy() { disposed = true; } };
}
