// Helper DOM. h('div', {class:'x', onclick: fn, dataset:{a:1}, style:{color:'red'}}, 'testo', altroNodo)
export function h(tag, props = {}, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(props || {})) {
    if (v === undefined || v === null || v === false) continue;
    if (k === 'class') el.className = v;
    else if (k === 'dataset') Object.assign(el.dataset, v);
    else if (k === 'style' && typeof v === 'object') Object.assign(el.style, v);
    else if (k.startsWith('on') && typeof v === 'function') el.addEventListener(k.slice(2).toLowerCase(), v);
    else if (k in el && k !== 'list' && typeof v !== 'string') el[k] = v;
    else el.setAttribute(k, v === true ? '' : v);
  }
  append(el, children);
  return el;
}

export function append(el, children) {
  for (const c of children.flat(Infinity)) {
    if (c === null || c === undefined || c === false) continue;
    el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return el;
}

/** Icona Material Symbols dentro md-icon: icon('home'), icon('favorite', {filled:true}) */
export function icon(name, { filled = false, slot } = {}) {
  const i = document.createElement('md-icon');
  if (slot) i.slot = slot;
  const s = document.createElement('span');
  s.className = 'material-symbols-outlined' + (filled ? ' filled' : '');
  s.textContent = name;
  i.append(s);
  return i;
}

export const $ = (sel, root = document) => root.querySelector(sel);
export const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

export function debounce(fn, ms = 250) {
  let t;
  return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); };
}
