// Bootstrap: componenti M3, tema, shell (con ripiego minimale se shell.js non c'e'), router.
import '../vendor/material-web.js';
import { initTheme, store } from './store.js';
import { api, onEvent } from './api.js';
import { startRouter } from './router.js';
import { mountTitlebar } from './ui/titlebar.js';
import { errorState } from './ui/empty-state.js';

initTheme();
mountTitlebar();
const app = document.getElementById('app');
const splash = document.getElementById('fs-splash');
const splashStatus = document.getElementById('fs-splash-status');
const BOOT_START = performance.now();
const MIN_SPLASH_MS = 2400;

function setSplashStatus(text) {
  if (splashStatus) splashStatus.textContent = text;
}

async function dismissSplash() {
  if (!splash) return;
  const elapsed = performance.now() - BOOT_START;
  const reduced = matchMedia('(prefers-reduced-motion: reduce)').matches;
  if (!reduced && elapsed < MIN_SPLASH_MS) {
    await new Promise((r) => setTimeout(r, MIN_SPLASH_MS - elapsed));
  }
  splash.classList.add('is-leaving');
  const done = () => splash.remove();
  if (reduced) return done();
  splash.addEventListener('transitionend', done, { once: true });
  setTimeout(done, 580);
}

async function fetchStatusWithRetry() {
  // Backoff crescente (~25 s in totale): l'eseguibile al primo avvio puo' impiegare piu' del previsto.
  const delays = [0, 350, 700, 1200, 2000, 3000, 4000, 5000, 6000, 8000];
  for (let i = 0; i < delays.length; i++) {
    if (delays[i]) {
      setSplashStatus('Attesa del server locale…');
      await new Promise((r) => setTimeout(r, delays[i]));
    }
    try {
      return await api.get('/status');
    } catch (e) {
      if (i === delays.length - 1) console.warn('status non disponibile:', e.message);
    }
  }
  return null;
}

async function boot() {
  setSplashStatus('Caricamento dell’interfaccia…');
  let outlet;
  try {
    const shell = await import('./shell.js');
    outlet = await shell.mountShell(app); // deve ritornare l'elemento in cui il router monta le view
  } catch (e) {
    console.warn('shell.js non disponibile, uso il ripiego minimale:', e.message);
    app.innerHTML = '<main id="view" class="view-outlet" style="padding:16px"></main>';
    outlet = app.querySelector('#view');
  }
  setSplashStatus('Lettura del catalogo…');
  // Lo stato va letto prima del primo render: su un'installazione nuova si va diretti all'onboarding,
  // senza montare e interrogare la home.
  const status = await fetchStatusWithRetry();
  if (status) store.set('status', status);
  if (!status) {
    outlet.replaceChildren(errorState({
      title: 'Impossibile collegarsi al server',
      message: 'Controlla che PhotoScan sia in funzione, poi riprova.',
      onRetry: () => location.reload(),
    }));
    await dismissSplash();
    // Se il server torna disponibile (evento SSE), l'app riparte da sola.
    let resumed = false;
    onEvent('connection', async ({ online }) => {
      if (!online || resumed) return;
      resumed = true;
      try {
        const st = await api.get('/status');
        store.set('status', st);
        outlet.replaceChildren();
        start(st, outlet);
      } catch { resumed = false; }
    });
    return;
  }
  start(status, outlet);
}

function start(status, outlet) {
  if (status.first_run && location.hash !== '#/onboarding') location.hash = '#/onboarding';
  setSplashStatus('Pronto');
  startRouter(outlet);
  dismissSplash();
  if (!status.first_run) import('./ui/update-notice.js').then((m) => m.watchUpdates()).catch(() => {});
}
boot().catch((e) => { console.error('Avvio non riuscito:', e); splash?.remove(); });
