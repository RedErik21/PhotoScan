// Dialog "Aiuta lo Sviluppatore": supporto volontario al progetto (nessun paywall, nessuna richiesta di rete dal backend).
import { h, icon } from './el.js';

/**
 * Link di supporto configurabili.
 * Incolla uno o più URL personali (es. Ko-fi, Buy Me a Coffee, PayPal, GitHub Sponsors):
 * i pulsanti corrispondenti compaiono automaticamente nel dialog.
 */
export const SUPPORT_LINKS = {
  kofi: '',            // es. 'https://ko-fi.com/tuonome'
  buymeacoffee: '',    // es. 'https://buymeacoffee.com/tuonome'
  paypal: 'https://paypal.me/AndreaStella825',
  githubSponsors: '',  // es. 'https://github.com/sponsors/RedErik21'
  githubRepo: 'https://github.com/RedErik21/PhotoScan',
  githubIssues: 'https://github.com/RedErik21/PhotoScan/issues',
};

const DONATION_PROVIDERS = [
  { key: 'kofi',           label: 'Offri un caffè su Ko-fi',        icon: 'favorite' },
  { key: 'buymeacoffee',   label: 'Buy Me a Coffee',                icon: 'favorite' },
  { key: 'paypal',         label: 'Dona con PayPal',                icon: 'favorite' },
  { key: 'githubSponsors', label: 'GitHub Sponsors',                icon: 'favorite' },
];

const STYLE_ID = 'fs-support-style';
const CSS = `
.fs-support-dialog { --md-dialog-container-max-width: 460px; }
.fs-support-head {
  display: flex; align-items: center; gap: var(--sp-3, 12px);
}
.fs-support-badge {
  display: inline-flex; align-items: center; justify-content: center; flex: none;
  width: 40px; height: 40px; border-radius: var(--shape-full, 999px);
  background: var(--md-sys-color-tertiary-container);
  color: var(--md-sys-color-on-tertiary-container);
}
.fs-support-badge md-icon {
  --md-icon-size: 22px;
  display: flex; align-items: center; justify-content: center;
  width: 22px; height: 22px; line-height: 1;
}
.fs-support-badge .material-symbols-outlined {
  display: block; font-size: 22px; line-height: 1;
  width: 22px; height: 22px; text-align: center;
  font-variation-settings: 'FILL' 1, 'wght' 500;
}
.fs-support-body {
  display: flex; flex-direction: column; gap: var(--sp-4, 16px);
  min-width: min(380px, 80vw);
}
.fs-support-lead {
  margin: 0; color: var(--md-sys-color-on-surface);
  font: 400 0.9375rem/1.55 var(--md-ref-typeface-plain);
}
.fs-support-note {
  margin: 0; color: var(--md-sys-color-on-surface-variant);
  font: 400 0.875rem/1.5 var(--md-ref-typeface-plain);
}
.fs-support-actions {
  display: flex; flex-direction: column; gap: var(--sp-2, 8px);
}
.fs-support-actions md-filled-button,
.fs-support-actions md-filled-tonal-button,
.fs-support-actions md-outlined-button {
  width: 100%;
}
.fs-support-secondary {
  display: flex; flex-wrap: wrap; gap: var(--sp-2, 8px);
  padding-top: var(--sp-2, 8px);
  border-top: 1px solid var(--md-sys-color-outline-variant);
}
.fs-support-secondary > * { flex: 1 1 160px; }
`;

function injectStyle() {
  if (document.getElementById(STYLE_ID)) return;
  const style = document.createElement('style');
  style.id = STYLE_ID;
  style.textContent = CSS;
  document.head.append(style);
}

function externalButton(tag, { href, label, ic }) {
  return h(tag, {
    href,
    target: '_blank',
    rel: 'noopener noreferrer',
  }, icon(ic, { slot: 'icon' }), label);
}

/** Apre il dialog "Aiuta lo Sviluppatore". */
export function openSupportDialog() {
  injectStyle();
  const dlg = document.createElement('md-dialog');
  dlg.className = 'fs-dialog fs-support-dialog';

  const donationItems = DONATION_PROVIDERS
    .map((p) => ({ ...p, url: String(SUPPORT_LINKS[p.key] || '').trim() }))
    .filter((p) => /^https?:\/\//i.test(p.url));

  const donationBlock = donationItems.length
    ? h('div', { class: 'fs-support-actions' },
      donationItems.map((p, idx) => externalButton(
        idx === 0 ? 'md-filled-button' : 'md-filled-tonal-button',
        { href: p.url, label: p.label, ic: p.icon },
      )))
    : null;

  const secondaryBtns = [];
  if (SUPPORT_LINKS.githubRepo) {
    secondaryBtns.push(externalButton(
      donationItems.length ? 'md-outlined-button' : 'md-filled-tonal-button',
      { href: SUPPORT_LINKS.githubRepo, label: 'Stella su GitHub', ic: 'open_in_new' },
    ));
  }
  if (SUPPORT_LINKS.githubIssues) {
    secondaryBtns.push(externalButton('md-outlined-button', {
      href: SUPPORT_LINKS.githubIssues, label: 'Segnala un’idea', ic: 'open_in_new',
    }));
  }

  const head = h('div', { slot: 'headline', class: 'fs-support-head' },
    h('span', { class: 'fs-support-badge', 'aria-hidden': 'true' }, icon('favorite', { filled: true })),
    h('span', {}, 'Aiuta lo Sviluppatore'));

  const body = h('div', { slot: 'content', class: 'fs-support-body' },
    h('p', { class: 'fs-support-lead' },
      'PhotoScan è un progetto indipendente, gratuito e 100% locale: nessuna pubblicità, nessun cloud e nessun tracciamento.'),
    h('p', { class: 'fs-support-note' },
      'Se ti fa risparmiare tempo nel mettere ordine tra le tue foto, puoi sostenerne lo sviluppo con un contributo volontario oppure aiutando il progetto su GitHub.'),
    donationBlock,
    secondaryBtns.length ? h('div', { class: 'fs-support-secondary' }, ...secondaryBtns) : null);

  const actions = h('div', { slot: 'actions' },
    h('md-text-button', { onclick: () => dlg.close('close') }, 'Chiudi'));

  dlg.append(head, body, actions);
  dlg.addEventListener('closed', () => dlg.remove(), { once: true });
  document.body.append(dlg);
  dlg.show();
}
