// Avvisi brevi (toast) basati su SweetAlert2. Stessa API di prima: showSnackbar(messaggio, opzioni).
// Uno alla volta, gli altri in coda. Se e' aperto un avviso modale (conferma), il toast attende.
import { swalReady, swalBase, toTopLayer } from './swal.js';

const queue = [];
const MAX_QUEUE = 3;       // oltre, si scartano i messaggi informativi piu' recenti (gli errori restano)
let showing = false;
let current = null;

/**
 * @param {string} message testo (mai HTML)
 * @param {{actionLabel?:string, onAction?:()=>void, duration?:number, error?:boolean}} [opts]
 *        duration in ms (0 = resta finche' non si chiude)
 */
export function showSnackbar(message, { actionLabel, onAction, duration = 4000, error = false } = {}) {
  const item = { message: String(message ?? ''), actionLabel, onAction, duration, error };
  if (current?.message === item.message || queue.some((q) => q.message === item.message)) return;
  if (error) queue.unshift(item); else queue.push(item);
  while (queue.length > MAX_QUEUE) queue.pop();
  if (!showing) next();
}

function next() {
  const item = queue.shift();
  if (!item) { showing = false; current = null; return; }
  showing = true;
  current = item;
  mount(item);
}

async function mount(item) {
  let Swal;
  try { Swal = await swalReady(); } catch { next(); return; }   // libreria assente: niente toast, ma la coda non si blocca
  // Un modale (conferma) e' aperto: Swal ne ammette uno solo, riprova dopo.
  if (Swal.isVisible() && !Swal.getPopup()?.classList.contains('swal2-toast')) {
    setTimeout(() => mount(item), 400);
    return;
  }
  const hasAction = !!item.actionLabel;
  const { heightAuto, returnFocus, ...base } = swalBase();   // incompatibili con i toast
  const iconName = item.error ? 'error' : 'check_circle';
  const res = await Swal.fire({
    ...base,
    toast: true,
    position: 'top-end',
    icon: item.error ? 'error' : 'success',
    iconHtml: `<span class="material-symbols-outlined fs-swal-toast-ic" aria-hidden="true">${iconName}</span>`,
    titleText: item.message,   // testo, non HTML: i messaggi possono contenere nomi scelti dall'utente
    showConfirmButton: hasAction,
    confirmButtonText: item.actionLabel,
    showCloseButton: item.duration <= 0 || item.error,
    timer: item.duration > 0 ? (item.error ? Math.max(item.duration, 6000) : item.duration) : undefined,
    timerProgressBar: item.duration > 0,
    didOpen: (popup) => {
      toTopLayer();
      popup.setAttribute('role', item.error ? 'alert' : 'status');
      popup.addEventListener('mouseenter', Swal.stopTimer);
      popup.addEventListener('mouseleave', Swal.resumeTimer);
    },
  });
  if (res.isConfirmed && hasAction) {
    try { item.onAction?.(); } catch (e) { console.error(e); }
  }
  next();
}
