// Supporto SweetAlert2 (vendor/sweetalert2, caricato con <script> classico in index.html).
const SRC = 'vendor/sweetalert2/sweetalert2.all.min.js';

let loading = null;
/** Risolve con window.Swal (carica lo script se non gia' presente). */
export function swalReady() {
  if (window.Swal) return Promise.resolve(window.Swal);
  loading ??= new Promise((resolve, reject) => {
    const s = document.createElement('script');
    s.src = SRC;
    s.onload = () => resolve(window.Swal);
    s.onerror = () => reject(new Error('SweetAlert2 non caricato'));
    document.head.append(s);
  });
  return loading;
}

/** Opzioni comuni: classi tema (css/swal.css), niente animazioni se reduced motion. */
export function swalBase() {
  return {
    buttonsStyling: false,
    heightAuto: false,
    returnFocus: true,
    customClass: {
      container: 'fs-swal-container',
      popup: 'fs-swal',
      title: 'fs-swal-title',
      htmlContainer: 'fs-swal-text',
      confirmButton: 'fs-swal-btn fs-swal-btn--primary',
      cancelButton: 'fs-swal-btn fs-swal-btn--text',
      denyButton: 'fs-swal-btn fs-swal-btn--text',
      closeButton: 'fs-swal-close',
      input: 'fs-swal-input',
    },
    showClass: matchMedia('(prefers-reduced-motion: reduce)').matches ? { popup: '', backdrop: '' } : undefined,
    hideClass: matchMedia('(prefers-reduced-motion: reduce)').matches ? { popup: '', backdrop: '' } : undefined,
  };
}

/** Se un md-dialog e' aperto (top layer), porta il container Swal sopra di lui con l'API popover. */
export function toTopLayer() {
  const c = document.querySelector('.swal2-container');
  if (!c || !document.querySelector('md-dialog[open]') || !c.showPopover) return;
  try {
    c.setAttribute('popover', 'manual');
    c.showPopover();
  } catch { /* gia' in top layer */ }
}
