// Wordmark calligrafico "PhotoScan" in stile Apple "hello" (SVG puro, nessun font esterno).
const SVG_NS = 'http://www.w3.org/2000/svg';

export const PHOTOSCAN_STROKES = [
  {
    id: 'p',
    d: 'M 42 134 C 54 92 66 52 78 28 C 92 20 132 22 132 50 C 132 78 98 92 64 90',
    delay: '0.12s',
    dur: '0.38s',
  },
  {
    id: 'hoto',
    d: 'M 140 100 C 156 72 176 36 172 24 C 168 16 154 28 146 62 C 138 96 134 118 132 128 C 140 102 160 84 172 90 C 182 96 170 124 184 128 C 194 130 204 118 212 104 C 220 86 244 84 246 102 C 248 120 226 132 214 124 C 204 116 212 92 234 92 C 248 92 258 106 268 94 C 278 74 286 50 290 36 C 284 68 276 106 280 124 C 284 132 296 126 306 108 C 314 86 338 84 340 102 C 342 120 320 132 308 124 C 298 116 306 92 328 92 C 342 92 348 106 356 104',
    delay: '0.44s',
    dur: '0.78s',
  },
  {
    id: 'tbar',
    d: 'M 268 66 C 280 64 294 62 306 61',
    delay: '1.16s',
    dur: '0.14s',
  },
  {
    id: 's',
    d: 'M 428 42 C 420 22 384 22 376 46 C 368 70 428 80 422 112 C 416 136 376 138 366 118',
    delay: '1.26s',
    dur: '0.32s',
  },
  {
    id: 'can',
    d: 'M 472 94 C 464 82 440 88 438 108 C 436 128 458 132 474 116 C 482 98 496 86 512 92 C 498 84 478 98 480 116 C 482 132 504 126 512 96 C 510 114 512 128 524 126 C 532 124 538 110 544 94 C 542 108 540 120 538 126 C 544 104 560 86 570 92 C 578 98 570 124 582 128 C 592 130 604 116 618 102',
    delay: '1.52s',
    dur: '0.52s',
  },
];

let gid = 0;

/**
 * Crea l'elemento SVG del wordmark corsivo PhotoScan.
 * variant: 'brand' (sidebar chiaro/scuro) | 'hero' (bianco/luminoso su sfondo scuro).
 */
export function createWordmark({ className = '', strokeWidth = 10, variant = 'brand', animated = false } = {}) {
  const gradId = `ps-wm-grad-${++gid}`;
  const svg = document.createElementNS(SVG_NS, 'svg');
  svg.setAttribute('viewBox', '0 0 660 170');
  svg.setAttribute('fill', 'none');
  svg.setAttribute('role', 'img');
  svg.setAttribute('aria-label', 'PhotoScan');
  svg.className.baseVal = ['ps-wordmark', `ps-wordmark--${variant}`, animated ? 'is-animated' : '', className].filter(Boolean).join(' ');

  const defs = document.createElementNS(SVG_NS, 'defs');
  const grad = document.createElementNS(SVG_NS, 'linearGradient');
  grad.setAttribute('id', gradId);
  grad.setAttribute('x1', '0%');
  grad.setAttribute('y1', '0%');
  grad.setAttribute('x2', '100%');
  grad.setAttribute('y2', '15%');
  const stops = variant === 'hero'
    ? [
        ['0%', '#ffffff'],
        ['55%', '#ebe7ff'],
        ['100%', '#ffe4c4'],
      ]
    : [
        ['0%', 'var(--md-sys-color-primary)'],
        ['65%', 'var(--md-sys-color-primary)'],
        ['100%', 'var(--md-sys-color-tertiary)'],
      ];
  for (const [off, col] of stops) {
    const st = document.createElementNS(SVG_NS, 'stop');
    st.setAttribute('offset', off);
    st.setAttribute('stop-color', col);
    grad.append(st);
  }
  defs.append(grad);
  svg.append(defs);

  for (const s of PHOTOSCAN_STROKES) {
    const p = document.createElementNS(SVG_NS, 'path');
    p.setAttribute('d', s.d);
    p.setAttribute('pathLength', '100');
    p.setAttribute('stroke', `url(#${gradId})`);
    p.setAttribute('stroke-width', String(strokeWidth));
    p.setAttribute('stroke-linecap', 'round');
    p.setAttribute('stroke-linejoin', 'round');
    p.classList.add('ps-wordmark__stroke', `ps-wordmark__stroke--${s.id}`);
    if (animated) {
      p.style.setProperty('--delay', s.delay);
      p.style.setProperty('--dur', s.dur);
    }
    svg.append(p);
  }
  return svg;
}
