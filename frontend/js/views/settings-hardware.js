// Impostazioni > Generale > «Il tuo computer»: configurazione rilevata, modello consigliato, foto al secondo previste.
// Dati da GET /api/hardware; la misura reale parte con POST /api/hardware/benchmark (job «hardware_bench»).
// Gli stili (.hw-*, .callout) sono in settings-general.js / components.css.
import { h, icon } from '../ui/el.js';
import { api, onEvent } from '../api.js';
import { skeletonList } from '../ui/skeleton.js';
import { showSnackbar } from '../ui/snackbar.js';
import { formatEta, formatNumber } from '../ui/format.js';

const JOB_NAME = 'hardware_bench';
const USES = { cuda: 'la scheda video NVIDIA', cpu: 'il processore', mps: 'il chip Apple' };
const fmt1 = (n) => n.toLocaleString('it-IT', { maximumFractionDigits: 1 });
const hint = (text) => h('p', { class: 'settings-hint md-typescale-body-medium' }, text);

/**
 * @param {HTMLElement} root contenitore (si riempie da solo)
 * @param {{current: () => {model_name: string, batch_size: string, workers: string}, onApply: (rec: object) => void}} opts
 */
export function mountHardware(root, { current, onApply }) {
  let disposed = false;
  let data = null;
  let jobId = null;
  let progress = null;
  const benchSlot = h('div', { class: 'settings-stack' });
  const body = h('div', { class: 'settings-stack' });
  root.replaceChildren(skeletonList(3));

  async function load() {
    try {
      data = await api.get('/hardware');
    } catch {
      if (!disposed) root.replaceChildren(hint('Impossibile rilevare la configurazione del computer.'));
      return;
    }
    if (disposed) return;
    if (data.benchmark_job_id && !jobId) { jobId = data.benchmark_job_id; progress = { message: 'Misura in corso…' }; }
    paint();
  }

  // ---- blocchi
  function specs(hw) {
    const gpu = hw.gpu
      ? `${hw.gpu.name} · ${fmt1(hw.gpu.vram_gb)} GB di memoria`
      : (hw.gpus[0] ? `${hw.gpus[0].name} (non utilizzabile)` : 'Nessuna scheda video dedicata');
    const cores = hw.cpu.cores ? `${hw.cpu.cores} core, ${hw.cpu.threads} thread` : `${hw.cpu.threads} thread`;
    return h('dl', { class: 'settings-info md-typescale-body-medium' },
      h('dt', {}, 'Scheda video'), h('dd', {}, gpu),
      h('dt', {}, 'Processore'), h('dd', {}, `${hw.cpu.name} (${cores})`),
      h('dt', {}, 'Memoria'), h('dd', {}, hw.ram_gb ? `${fmt1(hw.ram_gb)} GB` : '—'),
      h('dt', {}, 'Userà'), h('dd', {}, USES[hw.device] || hw.device));
  }

  function issues(hw) {
    return (hw.issues || []).map((i) => h('div', { class: 'callout callout--warning', role: 'note' },
      h('span', { class: 'callout__icon material-symbols-outlined', 'aria-hidden': 'true' }, 'warning'),
      h('div', { class: 'settings-stack' },
        h('p', {}, i.message),
        i.fix ? h('p', { class: 'md-typescale-body-small' }, 'Per risolvere: ', h('code', {}, i.fix)) : null)));
  }

  function recommendation(d) {
    const rec = d.recommendation;
    const row = d.models.find((m) => m.recommended) || d.models[0];
    const cur = current();
    const same = cur.model_name === rec.model && Number(cur.batch_size) === rec.batch_size && Number(cur.workers) === rec.workers;
    const apply = h('md-filled-tonal-button', { onclick: () => { onApply(rec); paint(); showSnackbar('Consiglio applicato: ricorda di salvare.'); } },
      icon('auto_fix_high', { slot: 'icon' }), 'Usa il consiglio');
    if (same) apply.disabled = true;
    return h('div', { class: 'callout callout--info', role: 'status' },
      h('span', { class: 'callout__icon material-symbols-outlined', 'aria-hidden': 'true' }, 'recommend'),
      h('div', { class: 'settings-stack' },
        h('p', { class: 'callout__title' }, `Consigliato per questo computer: ${row.label}`),
        h('p', {}, rec.reason),
        h('p', { class: 'md-typescale-body-small' },
          `Impostazioni suggerite: ${rec.batch_size} foto per volta, ${rec.workers} elaborazioni contemporanee.`),
        h('div', {}, apply),
        same ? h('p', { class: 'md-typescale-body-small' }, 'È già quello che hai selezionato.') : null));
  }

  function table(d) {
    const rows = d.models.map((m) => {
      const speed = m.per_s != null
        ? h('span', {}, `${fmt1(m.per_s)} foto/s`, h('span', { class: 'hw-sub md-typescale-body-small' }, m.estimated ? ' · stima' : ' · misurata'))
        : (m.error ? h('span', { class: 'hw-sub' }, m.error) : '—');
      const eta = m.eta_total_s != null ? formatEta(m.eta_total_s) || 'meno di 1 min' : '—';
      return h('div', { class: `hw-row md-typescale-body-medium${m.recommended ? ' hw-row--rec' : ''}`, role: 'row' },
        h('div', { class: 'hw-name', role: 'cell' }, m.label,
          m.recommended ? h('span', { class: 'hw-badge md-typescale-label-medium' }, 'Consigliato') : null,
          !m.fits ? h('span', { class: 'hw-badge hw-badge--warn md-typescale-label-medium' }, 'Non entra nella memoria') : null),
        h('div', { role: 'cell' }, m.fits ? speed : '—'),
        h('div', { role: 'cell' }, m.fits ? eta : '—'));
    });
    const lib = d.library;
    const measuredBound = d.models.find((m) => m.per_s != null && m.bottleneck === 'foto');
    return h('div', { class: 'settings-stack' },
      h('div', { class: 'hw-table', role: 'table', 'aria-label': 'Velocità prevista per modello' },
        h('div', { class: 'hw-row hw-row--head md-typescale-label-large', role: 'row' },
          h('div', { role: 'columnheader' }, 'Modello'),
          h('div', { role: 'columnheader' }, 'Velocità'),
          h('div', { role: 'columnheader' }, 'Tempo per tutto l’archivio')),
        ...rows),
      hint(`Tempo per analizzare ${formatNumber(lib.photos)} foto e ${formatNumber(lib.videos)} video `
        + `(ogni video conta ${lib.video_frames} fotogrammi). È una stima: i video richiedono un po’ più tempo.`),
      measuredBound
        ? hint('Su questo computer il limite è la lettura e la preparazione delle foto (disco e processore), non il calcolo: '
          + 'i modelli più grandi rallentano meno del previsto.')
        : null,
      !d.benchmark ? hint('Premi «Misura la velocità» per vedere quante foto al secondo analizza davvero il tuo computer.') : null,
      d.benchmark && d.benchmark.stale
        ? hint('Le misure salvate sono di un altro computer o di un’altra scheda video: rifalle.') : null);
  }

  function paintBench() {
    if (disposed || !data) return;
    if (jobId) {
      const total = progress?.total || 0;
      benchSlot.replaceChildren(h('div', { class: 'hw-progress', role: 'status' },
        h('p', { class: 'md-typescale-body-medium' }, `${progress?.message || 'Misura in corso…'}${total ? ` (${Math.min(progress.done || 0, total)} di ${total})` : ''}`),
        h('md-linear-progress', total ? { value: Math.min(1, (progress.done || 0) / total) } : { indeterminate: true }),
        h('div', {}, h('md-text-button', { onclick: cancelBench }, 'Annulla'))));
      return;
    }
    const noModel = data.models.every((m) => !m.cached);
    const btn = h('md-filled-tonal-button', { onclick: startBench }, icon('speed', { slot: 'icon' }), 'Misura la velocità');
    if (data.indexing_running || noModel) btn.disabled = true;
    benchSlot.replaceChildren(h('div', { class: 'hw-actions' }, btn),
      data.indexing_running ? hint('Ferma l’analisi per misurare: userebbe la scheda video insieme e la misura verrebbe falsata.')
        : noModel ? hint('Serve almeno un modello già scaricato.')
          : hint('Dura circa un minuto per modello. Non modifica le foto né l’archivio.'));
  }

  function paint() {
    if (disposed || !data) return;
    body.replaceChildren(specs(data.hardware), ...issues(data.hardware), recommendation(data), table(data));
    root.replaceChildren(body, benchSlot);
    paintBench();
  }

  async function startBench() {
    try {
      const r = await api.post('/hardware/benchmark', {});
      if (disposed) return;
      jobId = r.job_id;
      progress = { message: 'Avvio della misura…' };
      paintBench();
    } catch (e) {
      if (!disposed) showSnackbar(e.message || 'Misura non avviata.', { error: true });
    }
  }

  async function cancelBench() {
    if (!jobId) return;
    try { await api.post(`/jobs/${jobId}/cancel`, {}); } catch { /* il job finira' da solo */ }
  }

  const off = onEvent('job', (job) => {
    if (disposed || !job || job.name !== JOB_NAME) return;
    if (job.state === 'running') {
      jobId = job.id;
      progress = { message: job.message, done: job.done, total: job.total };
      paintBench();
      return;
    }
    jobId = null;
    progress = null;
    if (job.state === 'error') showSnackbar(job.error || 'Misura non riuscita.', { error: true });
    else if (job.state === 'done') showSnackbar('Misura completata.');
    load();
  });

  load();
  return { destroy() { disposed = true; off(); }, repaint() { paint(); } };
}
