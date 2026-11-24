// Barra del titolo propria: compare solo nella finestra desktop senza bordi (pywebview, vedi fotoscan/desktop.py).
// Nel browser normale non fa nulla. Trascinamento: classe pywebview-drag-region (gestita da pywebview).
import { h } from './el.js';
import { confirmDialog } from './dialog.js';

function api() {
  return window.pywebview?.api;
}

// Icone disegnate in SVG (non dal font): cosi' sono centrate allo stesso modo nei tre pulsanti.
const SVG_NS = 'http://www.w3.org/2000/svg';
const GLYPHS = {
  minimize: ['M1 5.5h8'],
  maximize: ['M1.5 1.5h7v7h-7z'],
  restore: ['M3.5 1.5h5v5', 'M1.5 3.5h5v5h-5z'],
  close: ['M1.5 1.5l7 7', 'M8.5 1.5l-7 7'],
};

function glyph(name) {
  const svg = document.createElementNS(SVG_NS, 'svg');
  svg.setAttribute('viewBox', '0 0 10 10');
  svg.setAttribute('width', '10');
  svg.setAttribute('height', '10');
  svg.setAttribute('aria-hidden', 'true');
  svg.setAttribute('focusable', 'false');
  for (const d of GLYPHS[name]) {
    const p = document.createElementNS(SVG_NS, 'path');
    p.setAttribute('d', d);
    p.setAttribute('fill', 'none');
    p.setAttribute('stroke', 'currentColor');
    p.setAttribute('stroke-width', '1');
    svg.append(p);
  }
  return svg;
}

function isDarkTheme() {
  const attr = document.documentElement.getAttribute('data-theme');
  if (attr === 'dark') return true;
  if (attr === 'light') return false;
  return window.matchMedia('(prefers-color-scheme: dark)').matches;
}

function syncNativeTitlebarTheme() {
  const w = api();
  if (!w?.set_theme) return;
  const push = () => { try { w.set_theme(isDarkTheme()); } catch { /* ignora */ } };
  push();
  const mo = new MutationObserver(push);
  mo.observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme'] });
  window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', push);
}

async function build() {
  const w = api();
  if (!w) return;
  syncNativeTitlebarTheme();
  // Se la finestra usa la barra nativa di Windows (frameless=False), non montiamo una seconda barra finta
  if (w.is_frameless) {
    try {
      if (!(await w.is_frameless())) return;
    } catch { /* fallback */ }
  }
  if (document.querySelector('.fs-titlebar')) return;
  document.documentElement.classList.add('is-desktop');

  const maxBtn = h('button', {
    class: 'fs-titlebar__btn', type: 'button', 'aria-label': 'Ingrandisci',
    onclick: async () => {
      const on = await api().toggle_maximize();
      maxBtn.replaceChildren(glyph(on ? 'restore' : 'maximize'));
      maxBtn.setAttribute('aria-label', on ? 'Ripristina' : 'Ingrandisci');
    },
  }, glyph('maximize'));

  const drag = h('div', { class: 'fs-titlebar__drag pywebview-drag-region' },
    h('span', { class: 'fs-titlebar__title' }, 'PhotoScan'));
  // Doppio clic sulla barra = ingrandisci / ripristina, come le finestre Windows
  drag.addEventListener('dblclick', () => maxBtn.click());

  const bar = h('header', { class: 'fs-titlebar' },
    drag,
    h('div', { class: 'fs-titlebar__controls' },
      h('button', {
        class: 'fs-titlebar__btn', type: 'button', 'aria-label': 'Riduci a icona',
        onclick: () => api().minimize(),
      }, glyph('minimize')),
      maxBtn,
      h('button', {
        class: 'fs-titlebar__btn fs-titlebar__btn--close', type: 'button', 'aria-label': 'Chiudi',
        onclick: async () => {
          const w = api();
          if (!w) return;
          try {
            const desc = w.busy_description ? await w.busy_description() : null;
            if (desc) {
              const ok = await confirmDialog({
                title: 'Chiudere PhotoScan?',
                message: `Un’operazione è in corso: ${desc}.\n\nChiudendo PhotoScan ora viene interrotta. L’analisi delle foto e dei volti riprenderà da dove si è fermata; la copia dal telefono salterà i file già copiati.`,
                confirmLabel: 'Chiudi comunque',
                cancelLabel: 'Resta',
                danger: true,
              });
              if (!ok) return;
            }
            if (w.force_close) await w.force_close();
            else await w.close();
          } catch {
            w.close?.();
          }
        },
      }, glyph('close'))));
  document.body.append(bar);
}

/** Da chiamare all'avvio: costruisce la barra appena pywebview e' pronto. */
export function mountTitlebar() {
  if (api()) build();
  else window.addEventListener('pywebviewready', build, { once: true });
}
