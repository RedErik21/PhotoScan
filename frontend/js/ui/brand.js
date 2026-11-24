// Wordmark calligrafico "PhotoScan" in stile Apple "hello" (font locale Playwrite US Trad, 100% offline).
const SVG_NS = 'http://www.w3.org/2000/svg';

let gid = 0;

/**
 * Crea l'elemento SVG del wordmark corsivo PhotoScan.
 * variant: 'brand' (sidebar chiaro/scuro) | 'hero' (bianco/luminoso su sfondo scuro).
 */
export function createWordmark({ className = '', variant = 'brand', animated = false } = {}) {
  const id = ++gid;
  const gradId = `ps-wm-grad-${id}`;
  const svg = document.createElementNS(SVG_NS, 'svg');
  svg.setAttribute('viewBox', '0 0 680 175');
  svg.setAttribute('fill', 'none');
  svg.setAttribute('role', 'img');
  svg.setAttribute('aria-label', 'PhotoScan');
  svg.className.baseVal = ['ps-wordmark', `ps-wordmark--${variant}`, animated ? 'is-animated' : '', className].filter(Boolean).join(' ');

  const defs = document.createElementNS(SVG_NS, 'defs');
  const grad = document.createElementNS(SVG_NS, 'linearGradient');
  grad.setAttribute('id', gradId);
  grad.setAttribute('gradientUnits', 'userSpaceOnUse');
  grad.setAttribute('x1', '40');
  grad.setAttribute('y1', '0');
  grad.setAttribute('x2', '640');
  grad.setAttribute('y2', '0');
  const stops = variant === 'hero'
    ? [
        ['0%', '#ffffff'],
        ['60%', '#eef0ff'],
        ['100%', '#f6e4d2'],
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

  let maskAttr = '';
  if (animated) {
    const featherId = `ps-wm-feather-${id}`;
    const maskId = `ps-wm-mask-${id}`;
    const fg = document.createElementNS(SVG_NS, 'linearGradient');
    fg.setAttribute('id', featherId);
    fg.setAttribute('x1', '0%');
    fg.setAttribute('y1', '0%');
    fg.setAttribute('x2', '100%');
    fg.setAttribute('y2', '0%');
    for (const [off, op] of [['0%', '1'], ['86%', '1'], ['100%', '0']]) {
      const s = document.createElementNS(SVG_NS, 'stop');
      s.setAttribute('offset', off);
      s.setAttribute('stop-color', '#ffffff');
      s.setAttribute('stop-opacity', op);
      fg.append(s);
    }
    defs.append(fg);

    const mask = document.createElementNS(SVG_NS, 'mask');
    mask.setAttribute('id', maskId);
    mask.setAttribute('maskUnits', 'userSpaceOnUse');
    mask.setAttribute('x', '0');
    mask.setAttribute('y', '0');
    mask.setAttribute('width', '680');
    mask.setAttribute('height', '175');
    const g = document.createElementNS(SVG_NS, 'g');
    g.setAttribute('transform', 'skewX(-14)');
    const r = document.createElementNS(SVG_NS, 'rect');
    r.setAttribute('class', 'fs-hello-reveal');
    r.setAttribute('x', '-80');
    r.setAttribute('y', '-20');
    r.setAttribute('width', '900');
    r.setAttribute('height', '220');
    r.setAttribute('fill', `url(#${featherId})`);
    g.append(r);
    mask.append(g);
    defs.append(mask);
    maskAttr = `url(#${maskId})`;
  }
  svg.append(defs);

  const text = document.createElementNS(SVG_NS, 'text');
  text.setAttribute('class', 'fs-hello-text');
  text.setAttribute('x', '340');
  text.setAttribute('y', '122');
  text.setAttribute('text-anchor', 'middle');
  text.setAttribute('fill', `url(#${gradId})`);
  if (maskAttr) text.setAttribute('mask', maskAttr);
  text.textContent = 'PhotoScan';
  svg.append(text);

  return svg;
}
