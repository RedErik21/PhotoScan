// Voci di navigazione condivise da shell (sidebar/rail/bottom bar).
// group: sezione della sidebar. step: passo del flusso "Organizza" (1..4). primary: nella barra inferiore compatta.
export const navItems = [
  { id: 'home',       group: 'library',  label: 'Home',          icon: 'home',                 path: '/',           primary: true },
  { id: 'gallery',    group: 'library',  label: 'Foto e video',  icon: 'photo_library',        path: '/gallery',    primary: true },
  { id: 'search',     group: 'library',  label: 'Cerca',         icon: 'search',               path: '/search',     primary: true },
  { id: 'albums',     group: 'library',  label: 'Album',         icon: 'collections_bookmark', path: '/albums',     primary: true },
  { id: 'people',     group: 'library',  label: 'Persone',       icon: 'face',                 path: '/people' },
  { id: 'places',     group: 'library',  label: 'Luoghi',        icon: 'map',                  path: '/places' },
  { id: 'stats',      group: 'library',  label: 'Statistiche',   icon: 'insights',             path: '/stats' },

  { id: 'organize',   group: 'organize', label: 'Organizza',     icon: 'auto_awesome_motion',  path: '/organize' },
  { id: 'phone',      group: 'import',   label: 'Telefono',      icon: 'smartphone',           path: '/phone' },
  { id: 'nas',        group: 'import',   label: 'NAS',           icon: 'dns',                  path: '/nas' },

  { id: 'settings',   group: 'system',   label: 'Impostazioni',  icon: 'settings',             path: '/settings' },
  { id: 'help',       group: 'system',   label: 'Aiuto',         icon: 'help',                 path: '/help' },
];

export const navGroups = [
  { id: 'library',  label: 'Libreria' },
  { id: 'organize', label: 'Strumenti' },
  { id: 'import',   label: 'Import' },
  { id: 'system',   label: 'Sistema' },
];

/** Passi del flusso Organizza (mantenuto per compatibilita' con moduli esterni). */
export const flowSteps = [];

/** Voce "Organizza" della barra inferiore compatta. */
export const organizeEntry = { id: 'organize', label: 'Organizza', icon: 'auto_awesome_motion', path: '/organize' };

const ORGANIZE_PATHS = ['/organize', '/index', '/duplicates', '/cleanup'];

/** Ritorna l'id di navItems attivo per un path (prefisso piu' lungo). */
export function activeNavId(path) {
  if (ORGANIZE_PATHS.some((p) => path === p || path.startsWith(p + '/'))) return 'organize';
  let best = null;
  for (const it of navItems) {
    if (it.path === '/' ? path === '/' : path === it.path || path.startsWith(it.path + '/')) {
      if (!best || it.path.length > best.path.length) best = it;
    }
  }
  return best ? best.id : null;
}

/** true se il path appartiene all'area Organizza. */
export function isFlowPath(path) {
  return activeNavId(path) === 'organize';
}
