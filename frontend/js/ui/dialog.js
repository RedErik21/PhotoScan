// Dialog M3 (md-dialog) con promise. Ogni dialog si crea, si apre, si chiude e si rimuove dal DOM.
// ESC e clic sullo sfondo annullano (la promise si risolve con il valore di annullamento).
import { h } from './el.js';
import { api, fileUrl } from '../api.js';
import { showSnackbar } from './snackbar.js';
import { swalReady, swalBase, toTopLayer } from './swal.js';

const rootStyle = () => getComputedStyle(document.documentElement);
const durToken = (name) => parseFloat(rootStyle().getPropertyValue(name)) || 0;
const easeToken = (name) => rootStyle().getPropertyValue(name).trim();
const reducedMq = matchMedia('(prefers-reduced-motion: reduce)');

/** Punto di origine (in % della finestra) al centro dell'elemento che ha aperto il dialog; 'center' se non disponibile. */
function originOf(el) {
  if (!(el instanceof HTMLElement) || el === document.body) return 'center';
  const r = el.getBoundingClientRect();
  if (!r.width && !r.height) return 'center';
  const x = ((r.left + r.width / 2) / innerWidth) * 100;
  const y = ((r.top + r.height / 2) / innerHeight) * 100;
  return `${x.toFixed(1)}% ${y.toFixed(1)}%`;
}

/**
 * Animazioni di apertura/chiusura di md-dialog (per istanza, sovrascrivono quelle di default del componente).
 * Apertura: scala 0.9 -> 1 + fade dall'origine del pulsante, --dur-medium, emphasized-decelerate.
 * Chiusura: piu' rapida (--dur-short), emphasized-accelerate. Reduced motion: solo dissolvenza.
 */
function applyDialogMotion(dlg, origin) {
  const reduced = reducedMq.matches;
  dlg.getOpenAnimation = () => ({
    dialog: [[reduced
      ? [{ opacity: 0 }, { opacity: 1 }]
      : [{ opacity: 0, transform: 'scale(.9)', transformOrigin: origin }, { opacity: 1, transform: 'scale(1)', transformOrigin: origin }],
    { duration: durToken('--dur-medium'), easing: easeToken('--motion-emphasized-decel') }]],
    scrim: [[[{ opacity: 0 }, { opacity: 0.32 }], { duration: durToken('--dur-medium'), easing: 'linear' }]],
  });
  dlg.getCloseAnimation = () => ({
    dialog: [[reduced
      ? [{ opacity: 1 }, { opacity: 0 }]
      : [{ opacity: 1, transform: 'scale(1)', transformOrigin: origin }, { opacity: 0, transform: 'scale(.94)', transformOrigin: origin }],
    { duration: durToken('--dur-short'), easing: easeToken('--motion-emphasized-accel') }]],
    scrim: [[[{ opacity: 0.32 }, { opacity: 0 }], { duration: durToken('--dur-short'), easing: 'linear' }]],
  });
}

/** Crea md-dialog, lo apre e risolve la promise alla chiusura. `build(ctx)` riempie il dialog. Stili in components.css. */
function runDialog({ build, cancelValue = null }) {
  return new Promise((resolve) => {
    const dlg = document.createElement('md-dialog');
    dlg.className = 'fs-dialog';
    applyDialogMotion(dlg, originOf(document.activeElement));
    let result = cancelValue;
    let locked = false;     // true durante operazioni in corso: ESC/scrim non chiudono
    let settled = false;

    const settle = () => {
      if (settled) return;
      settled = true;
      dlg.remove();
      resolve(result);
    };

    const ctx = {
      dlg,
      /** Chiude il dialog con un valore: la promise si risolve alla fine dell'animazione. */
      finish(value) { result = value; dlg.close(); },
      lock(on) { locked = !!on; },
    };

    dlg.addEventListener('cancel', (e) => { if (locked) e.preventDefault(); });
    dlg.addEventListener('closed', settle);

    try {
      build(ctx);
      document.body.append(dlg);
      dlg.show();
    } catch (e) {
      console.error(e);
      showSnackbar('Impossibile aprire la finestra', { error: true });
      settle();
    }
  });
}

/** Messaggio d'errore in italiano: il dettaglio del server se presente, altrimenti il ripiego. */
function errMsg(e, fallback) {
  return e?.body?.detail ? e.message : fallback;
}

/** Pulsante di conferma: colore di errore se `danger`. */
function confirmButton(label, danger, onClick) {
  const btn = document.createElement('md-filled-button');
  btn.textContent = label;
  if (danger) btn.className = 'fs-btn-danger';
  btn.addEventListener('click', onClick);
  return btn;
}

function cancelButton(label, onClick) {
  const btn = document.createElement('md-text-button');
  btn.textContent = label;
  btn.addEventListener('click', onClick);
  return btn;
}

function headline(text) {
  return h('div', { slot: 'headline' }, text);
}

/**
 * Conferma sì/no (SweetAlert2). ESC, clic fuori o annulla -> false.
 * @returns {Promise<boolean>}
 */
export async function confirmDialog({ title, message = '', confirmLabel = 'Conferma', cancelLabel = 'Annulla', danger = false } = {}) {
  const Swal = await swalReady();
  const res = await Swal.fire({
    ...swalBase(),
    icon: danger ? 'warning' : 'question',
    title: title ?? '',
    text: message,
    showCancelButton: true,
    reverseButtons: true,
    focusCancel: danger,
    confirmButtonText: confirmLabel,
    cancelButtonText: cancelLabel,
    customClass: { ...swalBase().customClass, confirmButton: 'fs-swal-btn fs-swal-btn--primary' + (danger ? ' is-danger' : '') },
    didOpen: toTopLayer,
  });
  return !!res.isConfirmed;
}

function iconSpan(name) {
  return h('span', { class: 'material-symbols-outlined', 'aria-hidden': 'true' }, name);
}

/**
 * Campo di testo con validazione e suggerimenti cliccabili.
 * @returns {Promise<string|null>} testo rifinito (trim) oppure null se annullato
 */
export async function promptDialog({ title, label = '', value = '', confirmLabel = 'OK', placeholder, suggestions, supportingText, error } = {}) {
  const Swal = await swalReady();
  const list = (suggestions || []).filter((s) => typeof s === 'string' && s.trim());
  const res = await Swal.fire({
    ...swalBase(),
    title: title ?? '',
    text: supportingText || undefined,
    input: 'text',
    inputLabel: label,
    inputValue: value ?? '',
    inputPlaceholder: placeholder || '',
    inputValidator: (v) => (String(v ?? '').trim() ? undefined : 'Inserisci un testo'),
    showCancelButton: true,
    reverseButtons: true,
    confirmButtonText: confirmLabel,
    cancelButtonText: 'Annulla',
    didOpen: () => {
      toTopLayer();
      const input = Swal.getInput();
      // `error`: messaggio iniziale (es. nome gia' usato); sparisce appena si modifica il testo.
      if (error) Swal.showValidationMessage(error);
      if (list.length) {
        const box = h('div', { class: 'fs-swal-chips', role: 'group', 'aria-label': 'Suggerimenti' },
          ...list.map((s) => h('button', {
            type: 'button', class: 'fs-swal-chip',
            onclick: () => { input.value = s; Swal.resetValidationMessage(); input.focus(); },
          }, s)));
        Swal.getHtmlContainer().append(box);
      }
      input.focus();
      input.select();
    },
  });
  return res.isConfirmed ? String(res.value).trim() : null;
}

/**
 * Scelta tra opzioni (radio). Confermare senza scelta non e' possibile.
 * @returns {Promise<*|null>} `value` dell'opzione scelta oppure null
 */
export async function choiceDialog({ title, message = '', options = [], confirmLabel = 'Conferma' } = {}) {
  const Swal = await swalReady();
  const group = h('div', { class: 'fs-swal-choices', role: 'radiogroup', 'aria-label': title ?? '' },
    ...options.map((opt, i) => h('label', { class: 'fs-swal-choice' },
      h('input', { type: 'radio', name: 'fs-choice', value: String(i) }),
      h('span', {},
        h('span', { class: 'fs-swal-choice__label' }, opt.label),
        opt.description ? h('span', { class: 'fs-swal-choice__desc' }, opt.description) : null))));
  const res = await Swal.fire({
    ...swalBase(),
    title: title ?? '',
    text: message || undefined,
    html: undefined,
    showCancelButton: true,
    reverseButtons: true,
    confirmButtonText: confirmLabel,
    cancelButtonText: 'Annulla',
    didOpen: () => {
      toTopLayer();
      Swal.getHtmlContainer().append(group);
      Swal.getHtmlContainer().style.display = 'block';
      const btn = Swal.getConfirmButton();
      btn.disabled = true;
      group.addEventListener('change', () => { btn.disabled = false; });
    },
    preConfirm: () => {
      const c = group.querySelector('input:checked');
      return c ? options[Number(c.value)].value : false;
    },
  });
  return res.isConfirmed && res.value !== false ? res.value : null;
}

/**
 * Scelta/creazione di un album e aggiunta dei file. Ritorna true se l'aggiunta e' riuscita.
 * @param {{fileIds: number[]}} args
 * @returns {Promise<boolean>}
 */
export function albumPickerDialog({ fileIds } = {}) {
  const ids = [...new Set(fileIds || [])];
  if (!ids.length) return Promise.resolve(false);

  return runDialog({
    cancelValue: false,
    build(ctx) {
      const { dlg } = ctx;
      let albums = [];
      let selected = null;       // indice in albums, oppure 'new'
      let createdId = null;      // album nuovo gia' creato (evita duplicati su nuovo tentativo)
      let createdName = '';
      let busy = false;

      const body = h('div', { slot: 'content', class: 'fs-album-body' });
      const confirm = confirmButton('Aggiungi', false, () => submit());
      confirm.disabled = true;
      const retry = cancelButton('Riprova', () => load());
      retry.style.display = 'none';
      // Annulla non si puo' premere mentre la richiesta e' in corso: altrimenti il dialog chiude ma le foto vengono aggiunte lo stesso.
      const cancel = cancelButton('Annulla', () => ctx.finish(false));
      const actions = h('div', { slot: 'actions' }, cancel, retry, confirm);
      dlg.append(headline('Aggiungi ad album'), body, actions);

      const setBusy = (on) => {
        busy = on;
        ctx.lock(on);
        cancel.disabled = on;
        retry.disabled = on;
        confirm.disabled = on || !canSubmit();
      };

      const canSubmit = () => {
        if (selected === 'new') return nameInput.value.trim().length > 0;
        return selected !== null;
      };

      const nameInput = document.createElement('md-outlined-text-field');
      nameInput.className = 'fs-dialog-field';
      nameInput.label = 'Nome del nuovo album';
      nameInput.maxLength = 120;
      nameInput.addEventListener('input', () => { confirm.disabled = busy || !canSubmit(); });
      nameInput.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' && !e.isComposing && canSubmit()) { e.preventDefault(); submit(); }
      });

      function selectRow(key) {
        selected = key;
        body.querySelectorAll('md-list-item').forEach((it) => {
          const on = it.dataset.key === String(key);
          it.setAttribute('aria-checked', String(on));
          it.querySelector('md-radio').checked = on;
        });
        nameInput.style.display = key === 'new' ? '' : 'none';
        if (key === 'new') requestAnimationFrame(() => nameInput.focus());
        confirm.disabled = busy || !canSubmit();
      }

      function row({ key, label, supporting, start }) {
        const radio = document.createElement('md-radio');
        radio.slot = 'start';
        radio.tabIndex = -1;
        radio.setAttribute('aria-hidden', 'true');
        const item = document.createElement('md-list-item');
        item.type = 'button';
        item.dataset.key = String(key);
        item.setAttribute('role', 'radio');
        item.setAttribute('aria-checked', 'false');
        item.append(start, radio, h('div', { slot: 'headline' }, label));
        if (supporting) item.append(h('div', { slot: 'supporting-text' }, supporting));
        // Dopo la creazione dell'album nuovo la scelta e' bloccata su di esso (evita album vuoti lasciati indietro).
        item.addEventListener('click', () => { if (!busy && (createdId === null || key === 'new')) selectRow(key); });
        return item;
      }

      function thumbFor(album) {
        const box = h('div', { class: 'fs-album-thumb', slot: 'start' });
        if (album.cover_id) {
          const img = h('img', { src: fileUrl.thumb(album.cover_id, 96), alt: '', loading: 'lazy', decoding: 'async' });
          box.append(img);
        } else {
          box.append(iconSpan('photo_album'));
        }
        return box;
      }

      function render() {
        body.replaceChildren();
        const list = h('md-list', { class: 'fs-dialog-list', role: 'radiogroup', 'aria-label': 'Album' });
        albums.forEach((a, i) => {
          const n = Number(a.count) || 0;
          list.append(row({
            key: i,
            label: a.name,
            supporting: n === 1 ? '1 foto' : `${n} foto`,
            start: thumbFor(a),
          }));
        });
        list.append(row({
          key: 'new',
          label: 'Nuovo album',
          supporting: 'Crea un album e aggiungi le foto',
          start: h('div', { class: 'fs-album-thumb', slot: 'start' }, iconSpan('add')),
        }));
        nameInput.style.display = 'none';
        body.append(list, nameInput);
        if (!albums.length) selectRow('new');
        else selectRow(null);
      }

      async function load() {
        retry.style.display = 'none';
        body.replaceChildren(h('div', { class: 'fs-dialog-busy' }, Object.assign(document.createElement('md-circular-progress'), { indeterminate: true })));
        confirm.disabled = true;
        try {
          albums = await api.get('/albums');
          if (!Array.isArray(albums)) throw new Error('Risposta non valida dal server');
          render();
        } catch (e) {
          const msg = errMsg(e, 'Impossibile caricare gli album');
          body.replaceChildren(h('p', { class: 'fs-dialog-error md-typescale-body-medium' }, msg));
          retry.style.display = '';
          showSnackbar(msg, { error: true });
        }
      }

      async function submit() {
        if (busy || !canSubmit()) return;
        setBusy(true);
        try {
          let albumId;
          let name;
          if (selected === 'new') {
            if (createdId === null) {
              name = nameInput.value.trim();
              const created = await api.post('/albums', { name });
              createdId = created.id;
              createdName = name;
              nameInput.disabled = true;
            }
            name = createdName;
            albumId = createdId;
          } else {
            albumId = albums[selected].id;
            name = albums[selected].name;
          }
          await api.post(`/albums/${albumId}/items`, { file_ids: ids });
          const n = ids.length;
          showSnackbar(n === 1 ? `Aggiunta 1 foto ad ${name}` : `Aggiunte ${n} foto ad ${name}`);
          setBusy(false);
          ctx.finish(true);
        } catch (e) {
          showSnackbar(errMsg(e, 'Aggiunta non riuscita'), { error: true });
          setBusy(false);
        }
      }

      load();
    },
  });
}
