// Sezione "Etichette" delle Impostazioni: editor delle etichette di contesto (zero-shot), prova con
// anteprima delle foto, sfondo generico. Le modifiche sono bozze: salva e ricalcolo stanno nel contenitore (settings.js).
// Contratto: mountSection(container, {settings, defaults, draft, onDirty}) -> {destroy, setServerErrors, reveal}.
// Stili inline in <style id="fs-labels-style">.
import { h, icon } from '../ui/el.js';
import { api, ApiError } from '../api.js';
import { showSnackbar } from '../ui/snackbar.js';
import { createTile } from '../ui/photo-tile.js';
import { skeletonGrid } from '../ui/skeleton.js';
import { emptyState } from '../ui/empty-state.js';
import { formatNumber } from '../ui/format.js';

const NAME_MAX = 60;
const PROMPT_MAX = 200;
const MAX_LABELS = 60;
const MAX_NULL = 50;
const RETRY_BUDGET_MS = 90000;          // il primo caricamento del modello di ricerca dura circa 25 s
const UNAVAILABLE_RE = /non e' disponibile|non è disponibile/i;
const STYLE_ID = 'fs-labels-style';
// Durate in linea con i token: --dur-short 150ms, --dur-medium 250ms; curva --motion-standard.
const DUR_SHORT = 150;
const DUR_MEDIUM = 250;
const EASE_STANDARD = 'cubic-bezier(.2, 0, 0, 1)';
const NAME_PLACEHOLDER = 'Es. Mare e spiaggia';
const PROMPT_PLACEHOLDER = 'Es. una foto al mare, sulla spiaggia';

const CSS = `
.fs-labels { display: flex; flex-direction: column; gap: var(--sp-4); min-width: 0; }
.fs-labels__intro { margin: 0; color: var(--md-sys-color-on-surface-variant); }
.fs-labels__section { display: flex; flex-direction: column; gap: var(--sp-3); min-width: 0; }
.fs-labels__section > h3 { margin: 0; }
.fs-labels__bar { display: flex; flex-wrap: wrap; align-items: center; justify-content: space-between; gap: var(--sp-2); }
.fs-labels__bar-actions { display: flex; flex-wrap: wrap; align-items: center; gap: var(--sp-2); }
.fs-labels__count { color: var(--md-sys-color-on-surface-variant); }
.fs-labels__list { display: flex; flex-direction: column; gap: var(--sp-2); }
.fs-labels__row {
  display: grid; grid-template-columns: minmax(180px, 1fr) minmax(0, 1.6fr) auto; gap: var(--sp-3); align-items: start;
  padding: var(--sp-3); border-radius: var(--shape-md); background: var(--md-sys-color-surface-container-low);
}
.fs-labels__field { width: 100%; }
.fs-labels__row-actions { display: flex; flex-wrap: wrap; align-items: center; justify-content: flex-end; gap: 2px; }
@media (max-width: 839px) {
  .fs-labels__row { grid-template-columns: minmax(0, 1fr); }
  .fs-labels__row-actions { justify-content: flex-start; }
}
/* Bersaglio di 48px su schermi tattili e nel layout a colonna. */
@media (pointer: coarse), (max-width: 839px) {
  .fs-labels__row-actions md-icon-button { width: 48px; height: 48px; }
}
.sugg-cards { display: grid; grid-template-columns: repeat(auto-fill, minmax(260px, 1fr)); gap: var(--sp-3); min-width: 0; }
.sugg-card {
  display: flex; flex-direction: column; gap: var(--sp-3); min-width: 0; padding: var(--sp-3);
  border-radius: var(--shape-md); background: var(--md-sys-color-surface-container-low);
}
.sugg-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 4px; }
.sugg-tile { aspect-ratio: 1; border-radius: var(--shape-xs); overflow: hidden; background: var(--md-sys-color-surface-container-high); }
.sugg-thumb { display: block; width: 100%; height: 100%; object-fit: cover; }
.sugg-thumb[hidden] { display: none; }
.sugg-meta { margin: 0; color: var(--md-sys-color-on-surface-variant); }
.sugg-actions { display: flex; flex-wrap: wrap; justify-content: flex-end; gap: var(--sp-2); }
.sugg-alt { align-self: flex-start; }
.sugg-progress { display: flex; flex-direction: column; gap: var(--sp-2); }
.sugg-progress > p { margin: 0; color: var(--md-sys-color-on-surface-variant); }
.sugg-bar { display: flex; flex-wrap: wrap; align-items: center; gap: var(--sp-3); }
.sugg-note { margin: 0; color: var(--md-sys-color-on-surface-variant); }
.sugg-visual { display: flex; flex-direction: column; gap: var(--sp-2); }
.sugg-vrow {
  display: flex; align-items: center; gap: var(--sp-3); min-width: 0; padding: var(--sp-2) var(--sp-3);
  border-radius: var(--shape-md); background: var(--md-sys-color-surface-container-low);
}
.sugg-vthumbs { display: flex; gap: 2px; flex: none; }
.sugg-vthumbs .sugg-tile { width: 40px; height: 40px; }
.sugg-vtext { display: flex; flex-direction: column; flex: 1; min-width: 0; }
.sugg-vname { overflow-wrap: anywhere; }
.fs-labels__adv { border-radius: var(--shape-md); background: var(--md-sys-color-surface-container-low); padding: 0 var(--sp-4); }
.fs-labels__adv > summary {
  min-height: 48px; display: flex; align-items: center; cursor: pointer;
  color: var(--md-sys-color-primary); font-weight: 500; border-radius: var(--shape-xs);
}
.fs-labels__adv > summary:focus-visible { outline: 2px solid var(--md-sys-color-primary); outline-offset: 2px; }
.fs-labels__adv-body { display: flex; flex-direction: column; gap: var(--sp-3); padding-bottom: var(--sp-4); }
.fs-labels__adv-body > h3 { margin: 0; }
.fs-labels__adv-body p { margin: 0; color: var(--md-sys-color-on-surface-variant); }
.fs-labels-tips ul { margin: var(--sp-1) 0 0; padding-left: 20px; }
.fs-labels-tips li + li { margin-top: 4px; }
.fs-labels-scrim {
  position: fixed; inset: 0; z-index: var(--z-overlay);
  background: color-mix(in srgb, var(--md-sys-color-scrim) 40%, transparent);
}
.fs-labels-sheet {
  position: fixed; z-index: var(--z-overlay); left: 50%; bottom: 0; transform: translateX(-50%);
  width: min(720px, 100%); max-height: 88vh; box-sizing: border-box;
  display: flex; flex-direction: column; padding: var(--sp-4);
  border-radius: var(--shape-xl) var(--shape-xl) 0 0;
  background: var(--md-sys-color-surface-container-high); color: var(--md-sys-color-on-surface);
  box-shadow: var(--elev-3);
  animation: fs-labels-rise var(--dur-medium, 250ms) var(--motion-emphasized-decel, ease-out);
}
@media (min-width: 600px) {
  .fs-labels-sheet { bottom: var(--sp-4); max-height: calc(100vh - 2 * var(--sp-4)); border-radius: var(--shape-xl); }
}
@keyframes fs-labels-rise { from { transform: translate(-50%, 24px); opacity: 0; } to { transform: translate(-50%, 0); opacity: 1; } }
@keyframes fs-labels-fade { from { opacity: 0; } to { opacity: 1; } }
@media (prefers-reduced-motion: reduce) { .fs-labels-sheet, .fs-labels-sheet__body > * { animation: none; } }
.fs-labels-sheet:focus { outline: none; }
.fs-labels-sheet__head { display: flex; align-items: center; gap: var(--sp-2); }
.fs-labels-sheet__head h2 { flex: 1; min-width: 0; margin: 0; overflow-wrap: anywhere; }
.fs-labels-sheet__intro { margin: 0 0 var(--sp-3); color: var(--md-sys-color-on-surface-variant); overflow-wrap: anywhere; }
.fs-labels-sheet__body {
  display: flex; flex-direction: column; gap: var(--sp-3); min-height: min(440px, 50vh);
  overflow-y: auto;
}
.fs-labels-sheet__body > * { animation: fs-labels-fade var(--dur-short, 150ms) ${EASE_STANDARD}; }
.fs-labels-sheet__grid { --tile-size: 96px; --tile-gap: 4px; }
`;

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

// Animazioni di riga (Element.animate). Con "riduci movimento" non si anima nulla.
const reduceMotion = () => !!window.matchMedia?.('(prefers-reduced-motion: reduce)').matches;
function animate(el, frames, duration) {
  if (!el || reduceMotion() || typeof el.animate !== 'function') return null;
  return el.animate(frames, { duration, easing: EASE_STANDARD });
}

// Etichetta rimossa nella sessione corrente e in attesa di "Annulla" dopo un cambio di scheda.
let stashedRestore = null;

function injectStyle() {
  if (document.getElementById(STYLE_ID)) return;
  const style = document.createElement('style');
  style.id = STYLE_ID;
  style.textContent = CSS;
  document.head.append(style);
}

/** Riquadro M3 (.callout): icona e contenuto. */
function callout(kind, iconName, ...content) {
  return h('div', { class: `callout callout--${kind}`, role: kind === 'error' ? 'alert' : 'status' },
    h('span', { class: 'material-symbols-outlined callout__icon', 'aria-hidden': 'true' }, iconName),
    h('div', {}, ...content));
}

const calloutTitle = (text) => h('p', { class: 'callout__title' }, text);

function textField({ label, value = '', placeholder = '', textarea = false, rows = 2 }) {
  const f = h('md-outlined-text-field', { label, type: textarea ? 'textarea' : 'text', class: 'fs-labels__field' });
  if (textarea) f.rows = rows;
  if (placeholder) f.placeholder = placeholder;
  f.value = value;
  return f;
}

/** Righe della descrizione: cresce con il testo (2-4) cosi' che il testo intero sia leggibile. */
const promptRows = (text) => Math.min(4, Math.max(2, Math.ceil(String(text).length / 36)));

function iconBtn(label, iconName, onClick, disabled = false) {
  return h('md-icon-button', { 'aria-label': label, title: label, disabled, onclick: onClick }, icon(iconName));
}

const cleanLabel = (it) => ({ name: it.name.trim(), prompt: it.prompt.trim() });

/**
 * Monta la sezione. `settings` = GET /settings (labels, null_prompts); `defaults` = GET /settings/defaults.
 * @returns {{destroy(): void, setServerErrors(fields): void, reveal(): void}}
 */
export function mountSection(container, { settings = {}, defaults = null, draft = null, onDirty = () => {} } = {}) {
  injectStyle();

  let alive = true;
  const savedLabels = (settings.labels ?? []).map(cleanLabel);
  const savedNulls = (settings.null_prompts ?? []).map((s) => String(s).trim()).filter(Boolean);
  const savedThreshold = Number(settings.score_threshold ?? 0.3);
  let draftLabels = (draft?.labels ?? settings.labels ?? []).map((l) => ({ name: l.name ?? '', prompt: l.prompt ?? '' }));
  let nullText = draft?.nullText ?? savedNulls.join('\n');
  let scoreThreshold = draft?.scoreThreshold ?? savedThreshold;
  let rowRefs = [];                            // riferimenti DOM allineati a draftLabels
  const refFor = new WeakMap();                // etichetta (oggetto della bozza) -> riferimenti DOM della riga
  let emptyEl = null;
  let busy = false;                            // un'animazione di uscita e' in corso
  let touched = new WeakSet();                 // etichette i cui campi hanno perso il focus almeno una volta
  let nullTouched = false;
  let revealAll = false;                       // dopo un tentativo di salvataggio: mostra tutti gli errori
  let closeSheet = null;
  const serverErr = {};                        // 'labels' | 'null_prompts' -> messaggio del server

  // ---------------------------------------------------------------- DOM statico
  const intro = h('p', { class: 'fs-labels__intro md-typescale-body-medium' },
    'Le etichette dicono di che cosa parla una foto. Ogni etichetta diventa una cartella del piano: '
    + 'le foto che la descrizione riconosce vengono raggruppate lì.');

  const tips = h('details', { class: 'fs-labels__adv' },
    h('summary', {}, 'Come scrivere una buona descrizione'),
    h('div', { class: 'fs-labels__adv-body fs-labels-tips md-typescale-body-medium' },
      h('ul', {},
        h('li', {}, 'Descrivi ciò che si vede, in frasi brevi e concrete: “una foto al mare, sulla spiaggia”.'),
        h('li', {}, 'Usa parole concrete: cibo, animali, neve, scontrino, torta di compleanno.'),
        h('li', {}, 'Evita termini generici come “persone”, “gente” o “foto”: compaiono quasi ovunque '
          + 'e farebbero finire troppe foto in quella cartella.'),
        h('li', {}, 'Dopo averla scritta, premi “Prova” per vedere quali foto verrebbero assegnate.'))));

  const countEl = h('span', { class: 'fs-labels__count md-typescale-label-large' });
  const addBtn = h('md-filled-tonal-button', { onclick: addLabel }, icon('add', { slot: 'icon' }), 'Nuova etichetta');
  const resetBtn = h('md-text-button', { onclick: resetLabels, disabled: !defaults },
    icon('restart_alt', { slot: 'icon' }), 'Ripristina etichette predefinite');
  const bar = h('div', { class: 'fs-labels__bar' }, countEl,
    h('div', { class: 'fs-labels__bar-actions' }, addBtn, resetBtn));

  const globalError = h('p', { class: 'callout callout--error', role: 'alert', hidden: true });
  const listEl = h('div', { class: 'fs-labels__list' });

  const nullField = h('md-outlined-text-field', {
    label: 'Descrizioni di sfondo (una per riga)', type: 'textarea', class: 'fs-labels__field',
  });
  nullField.rows = 5;
  nullField.value = nullText;
  nullField.addEventListener('input', () => { nullText = nullField.value; delete serverErr.null_prompts; refresh(); });
  nullField.addEventListener('focusout', () => { nullTouched = true; refresh(); });

  const thrValEl = h('strong', { class: 'tabular' }, `${Math.round(scoreThreshold * 100)}%`);
  const thrSlider = h('md-slider', {
    min: 0.1, max: 0.75, step: 0.05, value: scoreThreshold,
    'aria-label': 'Soglia minima di riconoscimento etichette',
  });
  thrSlider.addEventListener('input', () => {
    scoreThreshold = Number(Number(thrSlider.value).toFixed(2));
    thrValEl.textContent = `${Math.round(scoreThreshold * 100)}%`;
    refresh();
  });

  const advanced = h('details', { class: 'fs-labels__adv' },
    h('summary', {}, 'Opzioni avanzate'),
    h('div', { class: 'fs-labels__adv-body' },
      h('h3', { class: 'md-typescale-title-small' }, 'Soglia di riconoscimento'),
      h('p', { class: 'md-typescale-body-medium' },
        'Quanto deve essere sicuro il modello per assegnare una foto a un’etichetta: valori più alti sono più selettivi (attualmente: ',
        thrValEl, ').'),
      thrSlider,
      h('h3', { class: 'md-typescale-title-small' }, 'Sfondo generico'),
      h('p', { class: 'md-typescale-body-medium' },
        'Le descrizioni di sfondo rappresentano foto qualsiasi, senza un soggetto preciso, per esempio un muro '
        + 'bianco o un cielo grigio. Una foto viene assegnata a un’etichetta solo se somiglia all’etichetta '
        + 'più che allo sfondo. Cambiale solo se una cartella raccoglie troppe foto sbagliate.'),
      nullField));

  const root = h('div', { class: 'fs-labels' },
    intro, tips,
    h('section', { class: 'fs-labels__section' },
      h('h3', { class: 'section-title' }, 'Etichette di contesto'),
      bar, globalError, listEl, advanced));

  // ---------------------------------------------------------------- stato e validazione
  const nullLines = () => nullText.split('\n').map((s) => s.trim()).filter(Boolean);

  /** Errori per riga e globali. Le etichette non valide non entrano nella patch. */
  function computeErrors() {
    const rows = draftLabels.map(() => ({}));
    const seen = new Set();
    let global = '';
    if (draftLabels.length === 0) global = 'Serve almeno un’etichetta per organizzare le foto.';
    draftLabels.forEach((it, i) => {
      const e = rows[i];
      const name = it.name.trim();
      const prompt = it.prompt.trim();
      if (!name) e.name = 'Scrivi un nome.';
      else if (name.length > NAME_MAX) e.name = `Il nome è troppo lungo (massimo ${NAME_MAX} caratteri).`;
      else if (/[\\/]/.test(name) || name === '.' || name === '..') e.name = 'Il nome non può contenere / o \\.';
      else if (seen.has(name.toLowerCase())) e.name = 'Esiste già un’etichetta con questo nome.';
      else seen.add(name.toLowerCase());
      if (!prompt) e.prompt = 'Scrivi una descrizione per il modello.';
      else if (prompt.length > PROMPT_MAX) e.prompt = `La descrizione è troppo lunga (massimo ${PROMPT_MAX} caratteri).`;
    });
    const nl = nullLines();
    let nullErr = '';
    if (nl.length === 0) nullErr = 'Serve almeno una descrizione di sfondo.';
    else if (nl.length > MAX_NULL) nullErr = `Le descrizioni di sfondo sono al massimo ${MAX_NULL}.`;
    else if (nl.some((s) => s.length > PROMPT_MAX)) nullErr = `Ogni descrizione di sfondo può avere al massimo ${PROMPT_MAX} caratteri.`;
    const labelsOk = !global && rows.every((e) => !e.name && !e.prompt);
    return { rows, global, nullErr, labelsOk, nullsOk: !nullErr };
  }

  /** Patch con le sole parti valide e cambiate; stato sporco e invalido per il contenitore. */
  function compute() {
    const err = computeErrors();
    const patch = {};
    const labels = draftLabels.map(cleanLabel);
    if (err.labelsOk && JSON.stringify(labels) !== JSON.stringify(savedLabels)) patch.labels = labels;
    if (err.nullsOk && JSON.stringify(nullLines()) !== JSON.stringify(savedNulls)) patch.null_prompts = nullLines();
    if (Math.abs(scoreThreshold - savedThreshold) > 0.001) patch.score_threshold = scoreThreshold;
    const invalid = !(err.labelsOk && err.nullsOk);
    return { err, patch, invalid, dirty: Object.keys(patch).length > 0 || invalid };
  }

  let st = compute();

  /** Riallinea errori, pulsanti e contatori; poi comunica lo stato al contenitore. */
  function refresh() {
    if (!alive) return;
    st = compute();
    const err = st.err;
    rowRefs.forEach((ref, i) => {
      const item = draftLabels[i];
      const show = revealAll || touched.has(item);
      const e = err.rows[i];
      ref.name.error = show && !!e.name;
      ref.name.errorText = show && e.name ? e.name : '';
      ref.prompt.error = show && !!e.prompt;
      ref.prompt.errorText = show && e.prompt ? e.prompt : '';
      ref.up.disabled = i === 0;
      ref.down.disabled = i === draftLabels.length - 1;
      ref.dup.disabled = draftLabels.length >= MAX_LABELS;
    });
    const globalMsg = (revealAll && err.global) || serverErr.labels || '';
    globalError.hidden = !globalMsg;
    globalError.textContent = globalMsg;
    const nullMsg = (revealAll || nullTouched) && err.nullErr ? err.nullErr : (serverErr.null_prompts || '');
    nullField.error = !!nullMsg;
    nullField.errorText = nullMsg;

    countEl.textContent = `${formatNumber(draftLabels.length)} ${draftLabels.length === 1 ? 'etichetta' : 'etichette'}`;
    addBtn.disabled = draftLabels.length >= MAX_LABELS;
    resetBtn.disabled = !defaults;
    onDirty({ values: { labels: draftLabels.map((l) => ({ ...l })), nullText, scoreThreshold }, patch: st.patch, dirty: st.dirty, invalid: st.invalid });
  }

  // ---------------------------------------------------------------- elenco etichette
  /** Crea (una sola volta per etichetta) la riga DOM. Le azioni leggono la posizione al momento del clic. */
  function makeRow(item) {
    const name = textField({ label: 'Nome', value: item.name, placeholder: NAME_PLACEHOLDER });
    const prompt = textField({
      label: 'Descrizione per il modello', value: item.prompt, textarea: true, rows: promptRows(item.prompt),
      placeholder: PROMPT_PLACEHOLDER,
    });
    name.addEventListener('input', () => { item.name = name.value; delete serverErr.labels; refresh(); });
    prompt.addEventListener('input', () => {
      item.prompt = prompt.value;
      prompt.rows = promptRows(prompt.value);
      delete serverErr.labels;
      refresh();
    });
    for (const f of [name, prompt]) f.addEventListener('focusout', () => { touched.add(item); refresh(); });

    const testBtn = h('md-text-button', { onclick: () => openTestSheet(item, testBtn) },
      icon('science', { slot: 'icon' }), 'Prova');
    const up = iconBtn('Sposta su', 'arrow_upward', () => move(item, -1));
    const down = iconBtn('Sposta giù', 'arrow_downward', () => move(item, 1));
    const dup = iconBtn('Duplica etichetta', 'content_copy', () => duplicate(item));
    const del = iconBtn('Elimina etichetta', 'delete', () => removeItem(item));

    const el = h('div', { class: 'fs-labels__row' }, name, prompt,
      h('div', { class: 'fs-labels__row-actions' }, testBtn, up, down, dup, del));
    const ref = { item, el, name, prompt, up, down, dup, del, testBtn };
    refFor.set(item, ref);
    return ref;
  }

  /**
   * Riallinea il DOM all'ordine della bozza senza ricreare le righe esistenti (i campi mantengono
   * focus e testo). Gli spostamenti scorrono con FLIP; una riga nuova entra con una dissolvenza.
   * `focus` e' una funzione chiamata a riallineamento finito.
   */
  function syncRows({ focus = null, entering = null, removing = null } = {}) {
    const before = new Map(rowRefs.map((r) => [r.el, r.el.getBoundingClientRect().top]));
    if (removing) removing.remove();
    if (emptyEl) { emptyEl.remove(); emptyEl = null; }
    rowRefs = draftLabels.map((item) => refFor.get(item) || makeRow(item));
    for (const r of rowRefs) listEl.append(r.el);   // append sposta i nodi nell'ordine della bozza
    if (!rowRefs.length) {
      emptyEl = emptyState({
        icon: 'label_off', title: 'Nessuna etichetta',
        message: 'Aggiungi la prima etichetta per raggruppare le foto per tema.',
        actionLabel: 'Nuova etichetta', onAction: addLabel,
      });
      listEl.append(emptyEl);
    }
    for (const r of rowRefs) {
      if (r.item === entering) {
        animate(r.el, [{ opacity: 0, transform: 'translateY(-6px)' }, { opacity: 1, transform: 'none' }], DUR_MEDIUM);
      } else if (before.has(r.el)) {
        const dy = before.get(r.el) - r.el.getBoundingClientRect().top;
        if (dy) animate(r.el, [{ transform: `translateY(${dy}px)` }, { transform: 'none' }], DUR_MEDIUM);
      }
    }
    refresh();
    // Un campo appena creato si disegna solo dopo il suo aggiornamento: il focus va posticipato.
    if (focus) requestAnimationFrame(() => focus());
  }

  /** Ricostruisce tutta la lista (avvio e ripristino predefiniti). */
  function renderList() {
    listEl.replaceChildren();
    rowRefs = [];
    syncRows();
  }

  function addLabel() {
    if (draftLabels.length >= MAX_LABELS) {
      showSnackbar(`Le etichette sono al massimo ${MAX_LABELS}.`, { error: true });
      return;
    }
    const item = { name: '', prompt: '' };
    draftLabels.push(item);
    syncRows({ entering: item, focus: () => refFor.get(item)?.name.focus() });
  }

  function move(item, delta) {
    const i = draftLabels.indexOf(item);
    const j = i + delta;
    if (busy || i < 0 || j < 0 || j >= draftLabels.length) return;
    [draftLabels[i], draftLabels[j]] = [draftLabels[j], draftLabels[i]];
    syncRows({
      focus: () => {
        const r = refFor.get(item);
        if (!r) return;
        const primary = delta < 0 ? r.up : r.down;
        const other = delta < 0 ? r.down : r.up;
        (primary.disabled ? other : primary).focus();   // al bordo il pulsante opposto resta attivo
      },
    });
  }

  function duplicate(item) {
    if (draftLabels.length >= MAX_LABELS) return;
    const i = draftLabels.indexOf(item);
    if (i < 0) return;
    const base = `Copia di ${item.name.trim() || 'etichetta'}`.slice(0, NAME_MAX);
    let name = base;
    for (let n = 2; draftLabels.some((x) => x.name.trim().toLowerCase() === name.toLowerCase()); n++) {
      const suffix = ` ${n}`;
      name = base.slice(0, NAME_MAX - suffix.length) + suffix;
    }
    const copy = { name, prompt: item.prompt };
    draftLabels.splice(i + 1, 0, copy);
    syncRows({ entering: copy, focus: () => refFor.get(copy)?.name.focus() });
  }

  /** Elimina con animazione di uscita e pulsante "Annulla" (7 s). */
  function removeItem(item) {
    const ref = refFor.get(item);
    if (busy || !ref) return;
    busy = true;
    const finish = () => {
      busy = false;
      if (!alive) return;
      const i = draftLabels.indexOf(item);
      if (i < 0) return;
      draftLabels.splice(i, 1);
      refFor.delete(item);
      syncRows({
        removing: ref.el,
        focus: () => {
          const next = rowRefs[Math.min(i, rowRefs.length - 1)];
          (next ? next.name : addBtn).focus();
        },
      });
      showSnackbar(`Etichetta “${item.name.trim() || 'senza nome'}” eliminata.`, {
        actionLabel: 'Annulla',
        duration: 7000,
        onAction: () => restoreItem(item, i),
      });
    };
    const exit = animate(ref.el, [{ opacity: 1 }, { opacity: 0, transform: 'translateY(-4px)' }], DUR_SHORT);
    if (exit) exit.finished.then(finish, finish);
    else finish();
  }

  /** Riporta una etichetta eliminata. Se la scheda e' stata chiusa, la riporta alla riapertura. */
  function restoreItem(item, index) {
    if (!alive) {
      stashedRestore = { item, index };
      return;
    }
    if (draftLabels.includes(item)) return;
    draftLabels.splice(Math.min(index, draftLabels.length), 0, item);
    syncRows({ entering: item, focus: () => refFor.get(item)?.name.focus() });
  }

  /** Ripristina le etichette di fabbrica nella bozza (si salva con il pulsante del contenitore). */
  function resetLabels() {
    if (!defaults || !alive) return;
    draftLabels = (defaults.labels ?? []).map((l) => ({ name: l.name ?? '', prompt: l.prompt ?? '' }));
    touched = new WeakSet();
    revealAll = false;
    delete serverErr.labels;
    renderList();
    showSnackbar(st.patch.labels
      ? 'Etichette predefinite caricate: premi Salva per confermarle.'
      : 'Sono già le etichette predefinite.');
  }

  // ---------------------------------------------------------------- prova etichetta (bottom sheet)
  function openTestSheet(item, returnEl) {
    const prompt = item.prompt.trim();
    const name = item.name.trim() || 'senza nome';
    if (!prompt) {
      showSnackbar('Scrivi prima la descrizione dell’etichetta.', { error: true });
      return;
    }
    if (closeSheet) closeSheet();

    const titleId = 'fs-labels-sheet-title';
    let done = false;
    let viewerOpen = false;
    const body = h('div', { class: 'fs-labels-sheet__body', 'aria-live': 'polite' });
    const closeBtn = h('md-icon-button', { 'aria-label': 'Chiudi', onclick: () => close() }, icon('close'));
    const scrim = h('div', { class: 'fs-labels-scrim', onclick: () => close() });
    // La prova usa le impostazioni salvate: con modifiche non salvate lo dichiariamo.
    const unsaved = st.dirty
      ? h('p', { class: 'fs-labels-sheet__intro md-typescale-body-small' },
        'Hai modifiche non salvate: salva per provare i nuovi valori. Il risultato usa le impostazioni salvate.')
      : null;
    const sheet = h('div', {
      class: 'fs-labels-sheet', role: 'dialog', 'aria-modal': 'true', 'aria-labelledby': titleId, tabindex: '-1',
    },
    h('div', { class: 'fs-labels-sheet__head' },
      h('h2', { id: titleId, class: 'md-typescale-title-large' }, `Prova: ${name}`), closeBtn),
    h('p', { class: 'fs-labels-sheet__intro md-typescale-body-medium' },
      `Le foto più simili alla descrizione “${prompt}”.`),
    unsaved,
    body);

    const onKey = (e) => {
      if (e.key === 'Escape' && !viewerOpen) { e.preventDefault(); close(); }
    };
    const onFocusIn = (e) => {
      if (!viewerOpen && !sheet.contains(e.target)) closeBtn.focus();
    };
    function close() {
      if (done) return;
      done = true;
      document.removeEventListener('keydown', onKey, true);
      document.removeEventListener('focusin', onFocusIn, true);
      scrim.remove();
      sheet.remove();
      if (closeSheet === close) closeSheet = null;
      if (returnEl?.isConnected) returnEl.focus();
    }
    closeSheet = close;
    document.addEventListener('keydown', onKey, true);
    document.addEventListener('focusin', onFocusIn, true);
    document.body.append(scrim, sheet);
    closeBtn.focus();

    const loading = () => body.replaceChildren(
      callout('info', 'hourglass_top',
        calloutTitle('Sto preparando la ricerca…'),
        h('p', {}, 'La prima volta può richiedere circa 25 secondi.')),
      h('md-linear-progress', { indeterminate: true }),
      skeletonGrid(24, { tile: 96 }));

    const failed = (message) => body.replaceChildren(
      callout('error', 'error_outline', calloutTitle('Prova non riuscita'), h('p', {}, message)),
      h('div', {}, h('md-outlined-button', { onclick: () => { loading(); run(); } }, 'Riprova')));

    const showResult = (data) => {
      const items = data?.items ?? [];
      const above = data?.coverage?.above_threshold ?? 0;
      const total = data?.coverage?.total ?? 0;
      body.replaceChildren(
        callout('info', 'auto_awesome',
          calloutTitle(`Con le impostazioni salvate verrebbero assegnate ${formatNumber(above)} foto.`),
          h('p', {}, total ? `Su ${formatNumber(total)} foto e video analizzati nel catalogo.`
            : 'Il catalogo non ha ancora foto analizzate.')),
        items.length
          ? h('div', { class: 'tile-grid fs-labels-sheet__grid' },
            items.map((f, idx) => createTile(f, { size: 96, onOpen: () => openViewerAt(items, idx) })))
          : emptyState({ icon: 'image_search', title: 'Nessuna foto trovata' }));
    };

    async function openViewerAt(items, index) {
      viewerOpen = true;
      try {
        const { openViewer } = await import('./viewer.js');
        openViewer({ items, index, onClose: () => { viewerOpen = false; } });
      } catch {
        viewerOpen = false;
        showSnackbar('Visualizzatore non disponibile.', { error: true });
      }
    }

    // Il modello di ricerca puo' essere in caricamento (503): riprova con attesa crescente.
    async function run() {
      const started = Date.now();
      for (let attempt = 1; !done; attempt++) {
        try {
          const data = await api.post('/labels/test', { prompt });
          if (!done) showResult(data);
          return;
        } catch (e) {
          if (done) return;
          const retryable = e instanceof ApiError && e.status === 503 && !UNAVAILABLE_RE.test(e.message || '');
          if (!retryable || Date.now() - started > RETRY_BUDGET_MS) {
            failed(e?.message || 'Riprova tra un momento.');
            return;
          }
          await sleep(Math.min(1000 * attempt, 4000));
        }
      }
    }

    loading();
    run();
  }

  // ---------------------------------------------------------------- montaggio
  container.replaceChildren(root);
  renderList();
  // Un'eliminazione annullata dopo il cambio di scheda torna ora, con le altre modifiche in bozza.
  if (stashedRestore) {
    const { item, index } = stashedRestore;
    stashedRestore = null;
    if (!draftLabels.includes(item)) {
      draftLabels.splice(Math.min(index, draftLabels.length), 0, item);
      syncRows({ entering: item });
    }
  }

  return {
    destroy() {
      alive = false;
      if (closeSheet) closeSheet();
    },
    /** Errori del server per "labels" e "null_prompts". */
    setServerErrors(fields) {
      serverErr.labels = fields?.labels || '';
      serverErr.null_prompts = fields?.null_prompts || '';
      refresh();
    },
    /** Mostra subito tutti gli errori (dopo un salvataggio non possibile). */
    reveal() {
      revealAll = true;
      nullTouched = true;
      refresh();
    },
  };
}
