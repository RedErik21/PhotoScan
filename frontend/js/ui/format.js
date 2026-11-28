// Formattatori italiani condivisi.
const MESI = ['gennaio', 'febbraio', 'marzo', 'aprile', 'maggio', 'giugno', 'luglio', 'agosto', 'settembre', 'ottobre', 'novembre', 'dicembre'];
const GIORNI = ['domenica', 'lunedì', 'martedì', 'mercoledì', 'giovedì', 'venerdì', 'sabato'];
export const mesiNomi = MESI;

// Accetta anche 'YYYY-MM-DD HH:MM:SS' (spazio al posto di 'T', non parsato da Safari).
const parse = (iso) => (iso ? new Date(iso.length <= 10 ? iso + 'T00:00:00' : iso.replace(' ', 'T')) : null);

/** '2022-08-14T10:20:00' -> 'domenica 14 agosto 2022' */
export function formatDay(iso) {
  const d = parse(iso); if (!d || isNaN(d)) return 'Data sconosciuta';
  return `${GIORNI[d.getDay()]} ${d.getDate()} ${MESI[d.getMonth()]} ${d.getFullYear()}`;
}
/** -> '14 ago 2022' */
export function formatDateShort(iso) {
  const d = parse(iso); if (!d || isNaN(d)) return '—';
  return `${d.getDate()} ${MESI[d.getMonth()].slice(0, 3)} ${d.getFullYear()}`;
}
/** -> '14 agosto 2022, 10:20' */
export function formatDateTime(iso) {
  const d = parse(iso); if (!d || isNaN(d)) return '—';
  const hh = String(d.getHours()).padStart(2, '0'), mm = String(d.getMinutes()).padStart(2, '0');
  return `${d.getDate()} ${MESI[d.getMonth()]} ${d.getFullYear()}, ${hh}:${mm}`;
}
/** -> 'agosto 2022' */
export function formatMonth(year, month) { return `${MESI[(month || 1) - 1]} ${year}`; }

export function formatBytes(n) {
  if (n === null || n === undefined) return '—';
  if (n < 1024) return `${n} B`;
  const u = ['KB', 'MB', 'GB', 'TB']; let i = -1; let v = n;
  do { v /= 1024; i++; } while (v >= 1024 && i < u.length - 1);
  return `${v.toFixed(v >= 100 ? 0 : v >= 10 ? 1 : 2).replace('.', ',')} ${u[i]}`;
}
/** secondi -> '1:05' o '1:02:03' */
export function formatDuration(sec) {
  if (sec === null || sec === undefined || sec === '' || !isFinite(Number(sec)) || Number(sec) <= 0) return '';
  sec = Math.round(sec); const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
  const p = (x) => String(x).padStart(2, '0');
  return h ? `${h}:${p(m)}:${p(s)}` : `${m}:${p(s)}`;
}
export const formatNumber = (n) => (n ?? 0).toLocaleString('it-IT');
export function plural(n, one, many) { return `${formatNumber(n)} ${n === 1 ? one : many}`; }
/** secondi -> 'circa 5 min' */
export function formatEta(sec) {
  if (!isFinite(sec) || sec <= 0) return '';
  if (sec < 60) return 'meno di 1 min';
  if (sec < 3600) return `circa ${Math.round(sec / 60)} min`;
  return `circa ${Math.floor(sec / 3600)} h ${Math.round((sec % 3600) / 60)} min`;
}
/** secondi -> 'circa 5 min · fine verso le 14:25' (con data se non e' oggi) */
export function formatEtaEnd(sec) {
  const eta = formatEta(sec);
  if (!eta) return '';
  const end = new Date(Date.now() + sec * 1000);
  const hm = end.toLocaleTimeString('it-IT', { hour: '2-digit', minute: '2-digit' });
  const today = end.toDateString() === new Date().toDateString();
  const day = today ? '' : `${end.toLocaleDateString('it-IT', { weekday: 'short', day: 'numeric', month: 'short' })} `;
  return `${eta} · fine verso le ${day}${hm}`;
}

/** Nota per i file gia' spariti dal disco (campo `missing` di /cleanup/trash): tolti dal catalogo, non cestinati. */
export function missingNote(n) {
  if (!n) return '';
  return n === 1
    ? '1 file non c’era più sul disco: è stato tolto dal catalogo (non è finito nel Cestino).'
    : `${formatNumber(n)} file non c’erano più sul disco: sono stati tolti dal catalogo (non sono finiti nel Cestino).`;
}
