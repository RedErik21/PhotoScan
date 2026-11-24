// Impostazioni > Generale > «Per esperti»: calibrazione dei modelli AI (temperatura dei contesti, soglie della ricerca),
// preset dei modelli testati, modelli personalizzati da Hugging Face e calibrazione automatica sul proprio archivio.
// Dati da GET /api/models; salvataggi immediati (PUT/DELETE /api/models/profile, POST/DELETE /api/models/custom),
// indipendenti dalla barra «Salva» della pagina. Calibrazione: POST /api/models/calibrate (job «model_calibration»).
// Stili (.xp-*) in settings-general.js.
import { h, icon } from '../ui/el.js';
import { api, onEvent } from '../api.js';
import { confirmDialog } from '../ui/dialog.js';
import { skeletonList } from '../ui/skeleton.js';
import { showSnackbar } from '../ui/snackbar.js';
import { formatNumber } from '../ui/format.js';

const JOB_NAME = 'model_calibration';
const FIELDS = [
  { key: 'temp_factor', label: 'Fattore di temperatura', help: 'Più basso = etichette più decise (più foto nelle cartelle a tema).', step: '0.01' },
  { key: 'min_match', label: 'Somiglianza minima', help: 'Sotto questo valore un risultato della ricerca è rumore.', step: '0.001' },
  { key: 'min_gap', label: 'Stacco minimo', help: 'Quanto il migliore deve distaccarsi dalla media: scarta le ricerche senza senso.', step: '0.001' },
  { key: 'level_good', label: 'Soglia «buona»', help: 'Da qui un risultato è mostrato come buono.', step: '0.001' },
  { key: 'level_ok', label: 'Soglia «ottima»', help: 'Da qui un risultato è mostrato come ottimo.', step: '0.001' },
];
const SOURCE = {
  preset: { text: 'Calibrazione di fabbrica', cls: '' },
  base: { text: 'Non calibrato: usa i valori di Base', cls: ' hw-badge--warn' },
  manuale: { text: 'Modificata a mano', cls: ' hw-badge--warn' },
  automatica: { text: 'Calibrata sul tuo archivio', cls: '' },
};
const num = (d) => (x) => (x == null ? '—' : Number(x).toLocaleString('it-IT', { minimumFractionDigits: d, maximumFractionDigits: d }));
const n3 = num(3);   // soglie della ricerca
const n2 = num(2);   // temperature e fattori
const hint = (text) => h('p', { class: 'settings-hint md-typescale-body-medium' }, text);
const badge = (text, cls = '') => h('span', { class: `hw-badge md-typescale-label-medium${cls}` }, text);
const errText = (e) => String(e || '').replace(/^\w+Error: /, '');

const DET_SIZES = [
  { value: '480', label: '480 px — Più veloce (archivi molto grandi)' },
  { value: '640', label: '640 px — Bilanciato (predefinito)' },
  { value: '800', label: '800 px — Più preciso (volti piccoli, più lento)' },
  { value: '960', label: '960 px — Massima precisione (volti lontani, molto più lento)' },
];

/**
 * @param {HTMLElement} root contenitore (si riempie da solo)
 * @param {{onModelsChanged?: () => void}} opts onModelsChanged: elenco dei modelli cambiato (aggiunto/tolto)
 */
export function mountExpert(root, { onModelsChanged = () => {} } = {}) {
  let disposed = false;
  let data = null;
  let faceCfg = null;
  let faceDefaults = null;
  let editing = null;         // id del modello in modifica
  let job = null;             // {id, model, message, done, total}
  let adding = false;
  root.replaceChildren(skeletonList(3));

  async function load() {
    try {
      const [modelsData, curSettings, defSettings] = await Promise.all([
        api.get('/models'),
        api.get('/settings').catch(() => null),
        api.get('/settings/defaults').catch(() => null),
      ]);
      data = modelsData;
      faceCfg = curSettings;
      faceDefaults = defSettings;
    } catch {
      if (!disposed) root.replaceChildren(hint('Impossibile leggere i modelli.'));
      return;
    }
    if (disposed) return;
    if (data.calibration_job_id && !job) job = { id: data.calibration_job_id, model: data.calibration_model, message: 'Calibrazione in corso…' };
    paint();
  }

  // ---- valori
  function values(m) {
    const e = m.effective;
    return h('dl', { class: 'xp-values md-typescale-body-medium' },
      h('dt', {}, 'Temperatura etichette'),
      h('dd', {}, `${n2(e.temperature)} `, h('span', { class: 'hw-sub md-typescale-body-small' },
        `(${n2(data.score_temperature)} × fattore ${n2(e.temp_factor)})`)),
      h('dt', {}, 'Ricerca: minima · stacco'), h('dd', {}, `${n3(e.search_thr.min_match)} · ${n3(e.search_thr.min_gap)}`),
      h('dt', {}, 'Ricerca: buona · ottima'), h('dd', {}, `${n3(e.search_thr.level_good)} · ${n3(e.search_thr.level_ok)}`));
  }

  function editor(m) {
    const cur = { temp_factor: m.effective.temp_factor, ...m.effective.search_thr };
    const fields = {};
    const grid = h('div', { class: 'settings-numbers' }, FIELDS.map((f) => {
      const tf = document.createElement('md-outlined-text-field');
      tf.className = 'settings-field';
      tf.label = f.label;
      tf.type = 'number';
      tf.step = f.step;
      tf.value = String(cur[f.key]);
      const preset = f.key === 'temp_factor' ? m.preset.temp_factor : m.preset.search_thr[f.key];
      tf.supportingText = `${f.help} Fabbrica: ${f.key === 'temp_factor' ? n2(preset) : n3(preset)}.`;
      fields[f.key] = tf;
      return tf;
    }));
    async function save() {
      const body = { id: m.id, search_thr: {} };
      for (const f of FIELDS) {
        const v = Number(fields[f.key].value);
        if (!Number.isFinite(v)) { showSnackbar(`Valore non valido: ${f.label}.`, { error: true }); return; }
        if (f.key === 'temp_factor') body.temp_factor = v; else body.search_thr[f.key] = v;
      }
      try {
        await api.put('/models/profile', body);
        editing = null;
        showSnackbar('Calibrazione salvata. Ricalcola il piano per aggiornare le cartelle a tema.');
        load();
      } catch (e) { showSnackbar(e.message || 'Salvataggio non riuscito.', { error: true }); }
    }
    return h('div', { class: 'xp-editor settings-stack' }, grid,
      h('div', { class: 'hw-actions' },
        h('md-filled-tonal-button', { onclick: save }, icon('save', { slot: 'icon' }), 'Salva valori'),
        h('md-text-button', { onclick: () => { editing = null; paint(); } }, 'Annulla')));
  }

  function proposal(m) {
    const p = m.proposal;
    if (!p) return null;
    const c = p.check || {};
    const lines = [
      `Temperatura: fattore ${n2(p.temp_factor)} — assegna ${formatNumber(c.coverage)} foto su ${formatNumber(c.sample)} `
        + `(riferimento Base: ${formatNumber(c.coverage_ref)}), stessa etichetta nel ${Math.round((c.agreement || 0) * 100)}% dei casi.`,
      `Ricerca: minima ${n3(p.search_thr.min_match)}, buona ${n3(p.search_thr.level_good)}, ottima ${n3(p.search_thr.level_ok)}.`,
      c.gap_estimated
        ? `Stacco ${n3(p.search_thr.min_gap)}: stimato, perché il catalogo non è ancora analizzato con questo modello. Ricalibra dopo l’analisi completa.`
        : `Stacco ${n3(p.search_thr.min_gap)}: tiene ${c.real_kept} ricerche vere su ${c.real_total} e scarta ${c.junk_rejected} frasi senza senso su ${c.junk_total}.`,
    ];
    async function apply() {
      try {
        await api.put('/models/profile', { id: m.id, temp_factor: p.temp_factor, search_thr: p.search_thr, source: 'automatica' });
        showSnackbar('Calibrazione applicata. Ricalcola il piano per aggiornare le cartelle a tema.');
        load();
      } catch (e) { showSnackbar(e.message || 'Non applicata.', { error: true }); }
    }
    return h('div', { class: 'callout callout--info', role: 'status' },
      h('span', { class: 'callout__icon material-symbols-outlined', 'aria-hidden': 'true' }, 'tune'),
      h('div', { class: 'settings-stack' },
        h('p', { class: 'callout__title' }, 'Calibrazione proposta'),
        ...lines.map((t) => h('p', { class: 'md-typescale-body-small' }, t)),
        h('div', { class: 'hw-actions' },
          h('md-filled-tonal-button', { onclick: apply }, icon('check', { slot: 'icon' }), 'Applica'))));
  }

  function row(m) {
    const src = SOURCE[m.effective.source] || SOURCE.base;
    const busy = !!job;
    const actions = [];
    if (editing !== m.id) {
      actions.push(h('md-text-button', { onclick: () => { editing = m.id; paint(); } }, icon('edit', { slot: 'icon' }), 'Modifica'));
    }
    if (m.id !== data.reference_model) {
      const cal = h('md-text-button', { onclick: () => calibrate(m) }, icon('tune', { slot: 'icon' }), 'Calibra sul mio archivio');
      if (busy) cal.disabled = true;
      actions.push(cal);
    }
    if (m.override) actions.push(h('md-text-button', { onclick: () => reset(m) }, icon('restart_alt', { slot: 'icon' }), 'Ripristina'));
    if (m.custom && !m.current) actions.push(h('md-text-button', { onclick: () => remove(m) }, icon('delete', { slot: 'icon' }), 'Rimuovi'));
    const progress = job && job.model === m.id
      ? h('div', { class: 'hw-progress', role: 'status' },
        h('p', { class: 'md-typescale-body-medium' }, job.message || 'Calibrazione in corso…'),
        h('md-linear-progress', job.total ? { value: Math.min(1, (job.done || 0) / job.total) } : { indeterminate: true }),
        h('div', {}, h('md-text-button', { onclick: cancel }, 'Annulla')))
      : null;
    return h('div', { class: 'xp-model settings-stack' },
      h('div', { class: 'hw-name md-typescale-title-small' }, m.label,
        m.current ? badge('In uso') : null,
        m.custom ? badge('Personalizzato', ' hw-badge--warn') : null,
        badge(src.text, src.cls)),
      h('p', { class: 'hw-sub md-typescale-body-small' }, m.id, m.cached ? ' · scaricato' : ' · da scaricare', m.in_catalog ? ' · archivio analizzato con questo modello' : ''),
      editing === m.id ? editor(m) : values(m),
      proposal(m),
      progress,
      actions.length ? h('div', { class: 'hw-actions' }, actions) : null);
  }

  function addForm() {
    const idTf = document.createElement('md-outlined-text-field');
    idTf.className = 'settings-field';
    idTf.label = 'Identificativo Hugging Face';
    idTf.placeholder = 'autore/nome-modello';
    idTf.supportingText = `Famiglie supportate: ${data.supported_types.join(', ')}. Es. google/siglip2-base-patch16-512.`;
    const labelTf = document.createElement('md-outlined-text-field');
    labelTf.className = 'settings-field';
    labelTf.label = 'Nome da mostrare (facoltativo)';
    const btn = h('md-filled-tonal-button', {}, icon('add', { slot: 'icon' }), 'Aggiungi modello');
    btn.addEventListener('click', async () => {
      const id = idTf.value.trim();
      if (!id) { idTf.error = true; idTf.errorText = 'Scrivi l’identificativo del modello.'; return; }
      adding = true; btn.disabled = true;
      try {
        await api.post('/models/custom', { id, label: labelTf.value.trim() });
        showSnackbar('Modello aggiunto: sceglilo in «Modello AI e calcolo», poi calibralo.');
        onModelsChanged();
        load();
      } catch (e) {
        idTf.error = true; idTf.errorText = e.message || 'Modello non aggiunto.';
      } finally { adding = false; btn.disabled = false; }
    });
    idTf.addEventListener('input', () => { idTf.error = false; });
    return h('div', { class: 'settings-stack' },
      h('p', { class: 'md-typescale-title-small' }, 'Aggiungi un tuo modello'),
      hint('Qualsiasi modello testo-immagine di Hugging Face compatibile con transformers. Viene controllato subito '
        + '(scarica solo la descrizione del modello); i pesi si scaricano al primo utilizzo.'),
      h('div', { class: 'settings-numbers' }, idTf, labelTf),
      h('div', {}, btn));
  }

  function facesForm() {
    if (!faceCfg) return null;
    const defs = faceDefaults || {
      faces_det_size: 640,
      faces_threshold: 0.50,
      faces_assign_threshold: 0.52,
      faces_min_photos: 3,
    };

    const detSel = document.createElement('md-outlined-select');
    detSel.className = 'settings-field';
    detSel.label = 'Dimensione rilevamento volti (SCRFD)';
    detSel.supportingText = 'Più alta = trova volti più piccoli e lontani ma rallenta l’analisi. Vale per le nuove foto; usa «Rianalizza i volti» per le foto già analizzate.';
    const curDet = String(faceCfg.faces_det_size ?? 640);
    for (const opt of DET_SIZES) {
      const o = document.createElement('md-select-option');
      o.value = opt.value;
      if (opt.value === curDet) o.selected = true;
      o.append(h('div', { slot: 'headline' }, opt.label));
      detSel.append(o);
    }

    const mkNum = (label, val, step, min, max, help) => {
      const tf = document.createElement('md-outlined-text-field');
      tf.className = 'settings-field';
      tf.label = label;
      tf.type = 'number';
      tf.step = step;
      tf.min = String(min);
      tf.max = String(max);
      tf.value = String(val);
      tf.supportingText = help;
      return tf;
    };

    const thrTf = mkNum(
      'Soglia nuovi gruppi',
      faceCfg.faces_threshold ?? 0.50,
      '0.01', 0.2, 0.9,
      `Più alta = gruppi più puri (meno persone mescolate). Predefinito: ${n2(defs.faces_threshold)}.`,
    );
    const assignTf = mkNum(
      'Soglia assegnazione a persone esistenti',
      faceCfg.faces_assign_threshold ?? 0.52,
      '0.01', 0.2, 0.9,
      `Soglia conservativa per agganciare nuovi volti a chi hai già nominato. Predefinito: ${n2(defs.faces_assign_threshold)}.`,
    );
    const minTf = mkNum(
      'Foto minime per creare una persona',
      faceCfg.faces_min_photos ?? 3,
      '1', 1, 50,
      `Evita decine di gruppi per passanti comparsi una sola volta. Predefinito: ${defs.faces_min_photos}.`,
    );

    const readPatch = () => {
      const detSize = Number(detSel.value || curDet);
      const thr = Number(thrTf.value);
      const assignThr = Number(assignTf.value);
      const minPhotos = Number(minTf.value);
      if (!Number.isFinite(detSize) || !Number.isFinite(thr) || !Number.isFinite(assignThr) || !Number.isInteger(minPhotos)) {
        return null;
      }
      return {
        faces_det_size: detSize,
        faces_threshold: thr,
        faces_assign_threshold: assignThr,
        faces_min_photos: minPhotos,
      };
    };

    const saveBtn = h('md-filled-tonal-button', {}, icon('save', { slot: 'icon' }), 'Salva impostazioni volti');
    const resetBtn = h('md-text-button', {}, icon('restart_alt', { slot: 'icon' }), 'Ripristina predefiniti');
    const rescanBtn = h('md-outlined-button', {}, icon('refresh', { slot: 'icon' }), 'Rianalizza i volti');

    saveBtn.addEventListener('click', async () => {
      const patch = readPatch();
      if (!patch) { showSnackbar('Controlla i valori numerici dei volti.', { error: true }); return; }
      const detChanged = patch.faces_det_size !== Number(faceCfg.faces_det_size ?? 640);
      saveBtn.disabled = true;
      try {
        faceCfg = await api.put('/settings', patch);
        showSnackbar(detChanged
          ? 'Impostazioni volti salvate. Premi «Rianalizza i volti» per applicare la nuova dimensione anche alle foto già analizzate.'
          : 'Impostazioni volti salvate. Usa «Ricalcola gruppi» nella pagina Persone per applicare le nuove soglie.');
      } catch (e) {
        showSnackbar(e.message || 'Salvataggio impostazioni volti non riuscito.', { error: true });
      } finally {
        saveBtn.disabled = false;
      }
    });

    resetBtn.addEventListener('click', async () => {
      resetBtn.disabled = true;
      try {
        faceCfg = await api.put('/settings', {
          faces_det_size: defs.faces_det_size ?? 640,
          faces_threshold: defs.faces_threshold ?? 0.50,
          faces_assign_threshold: defs.faces_assign_threshold ?? 0.52,
          faces_min_photos: defs.faces_min_photos ?? 3,
        });
        showSnackbar('Impostazioni volti ripristinate ai valori predefiniti.');
        paint();
      } catch (e) {
        showSnackbar(e.message || 'Ripristino non riuscito.', { error: true });
      } finally {
        resetBtn.disabled = false;
      }
    });

    rescanBtn.addEventListener('click', async () => {
      const patch = readPatch();
      if (!patch) { showSnackbar('Controlla i valori numerici dei volti.', { error: true }); return; }
      const ok = await confirmDialog({
        title: 'Rianalizzare tutti i volti?',
        message: `Le foto già analizzate verranno rielaborate con dimensione ${patch.faces_det_size} px (può richiedere alcuni minuti). `
          + 'I nomi delle persone, i volti confermati a mano e quelli esclusi vengono mantenuti.',
        confirmLabel: 'Salva e rianalizza',
      });
      if (!ok || disposed) return;
      rescanBtn.disabled = true;
      try {
        faceCfg = await api.put('/settings', patch);
        await api.post('/people/reset-scan', {});
        await api.post('/people/scan', {});
        showSnackbar('Rianalisi dei volti avviata in background: puoi seguire l’avanzamento dalla barra laterale.');
      } catch (e) {
        showSnackbar(e.message || 'Impossibile avviare la rianalisi dei volti.', { error: true });
      } finally {
        rescanBtn.disabled = false;
      }
    });

    return h('div', { class: 'settings-stack' },
      h('p', { class: 'md-typescale-title-small' }, 'Riconoscimento volti (per esperti)'),
      hint('Regola il compromesso tra velocità e precisione del rilevamento volti, e le soglie con cui i volti vengono raggruppati in Persone.'),
      detSel,
      h('div', { class: 'settings-numbers' }, thrTf, assignTf, minTf),
      h('div', { class: 'hw-actions' }, saveBtn, resetBtn, rescanBtn));
  }

  function paint() {
    if (disposed || !data) return;
    root.replaceChildren(
      h('div', { class: 'callout callout--warning', role: 'note' },
        h('span', { class: 'callout__icon material-symbols-outlined', 'aria-hidden': 'true' }, 'science'),
        h('div', { class: 'settings-stack' },
          h('p', {}, 'Ogni modello misura la somiglianza su una scala diversa: queste soglie decidono cosa finisce nelle '
            + 'cartelle a tema e cosa trova la ricerca. Valori sbagliati possono svuotare le cartelle o riempire la ricerca di risultati casuali.'),
          h('p', { class: 'md-typescale-body-small' }, 'Le modifiche si salvano subito. Puoi sempre tornare alla calibrazione di fabbrica con «Ripristina».'))),
      hint('«Calibra sul mio archivio» confronta il modello con SigLIP 2 Base (tarato a mano) sulle stesse foto (3000 con la scheda video, 1000 sul processore) '
        + 'e propone i valori equivalenti. Serve SigLIP 2 Base scaricato; dura da uno a pochi minuti.'),
      h('div', { class: 'xp-list' }, data.models.map(row)),
      h('hr', {}),
      addForm(),
      faceCfg ? h('hr', {}) : null,
      facesForm());
  }

  async function calibrate(m) {
    if (!m.cached) {
      const ok = await confirmDialog({
        title: 'Scaricare il modello?',
        message: `Per calibrare «${m.label}» bisogna scaricarlo (può richiedere alcuni GB).`,
        confirmLabel: 'Scarica e calibra',
      });
      if (!ok || disposed) return;
    }
    try {
      const r = await api.post('/models/calibrate', { id: m.id });
      job = { id: r.job_id, model: m.id, message: 'Avvio della calibrazione…' };
      paint();
    } catch (e) { showSnackbar(e.message || 'Calibrazione non avviata.', { error: true }); }
  }

  async function cancel() {
    if (!job) return;
    try { await api.post(`/jobs/${job.id}/cancel`, {}); } catch { /* finira' da solo */ }
  }

  async function reset(m) {
    try {
      await api.del('/models/profile', { id: m.id });
      showSnackbar('Ripristinata la calibrazione di fabbrica.');
      load();
    } catch (e) { showSnackbar(e.message || 'Non ripristinata.', { error: true }); }
  }

  async function remove(m) {
    const ok = await confirmDialog({ title: `Rimuovere «${m.label}»?`, message: 'Il modello sparisce dall’elenco. I file già scaricati restano sul disco.', confirmLabel: 'Rimuovi', danger: true });
    if (!ok || disposed) return;
    try {
      await api.del('/models/custom', { id: m.id });
      onModelsChanged();
      load();
    } catch (e) { showSnackbar(e.message || 'Non rimosso.', { error: true }); }
  }

  const off = onEvent('job', (j) => {
    if (disposed || !j || j.name !== JOB_NAME) return;
    if (j.state === 'running') {
      job = { ...(job || {}), id: j.id, message: j.message, done: j.done, total: j.total };
      paint();
      return;
    }
    job = null;
    if (j.state === 'error') showSnackbar(errText(j.error) || 'Calibrazione non riuscita.', { error: true });
    else if (j.state === 'done') showSnackbar('Calibrazione completata: controlla la proposta e premi «Applica».');
    load();
  });

  load();
  return { destroy() { disposed = true; off(); }, reload: load, get busy() { return adding; } };
}
