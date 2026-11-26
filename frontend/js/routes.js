// Tabella delle route: ogni view e' un modulo che esporta `mount(root, ctx)` (vedi docs/CONTRACT.md).
// Non modificare senza coordinarsi: i file in views/ sono posseduti da agenti diversi.
export const routes = [
  { path: '/',                 view: './views/home.js',          title: 'Home' },
  { path: '/gallery',          view: './views/gallery.js',       title: 'Foto e video' },
  { path: '/search',           view: './views/search.js',        title: 'Cerca' },
  { path: '/organize',         view: './views/organize.js',      title: 'Organizza' },
  { path: '/cleanup',          view: './views/organize.js',      title: 'Organizza' },
  { path: '/duplicates',       view: './views/organize.js',      title: 'Organizza' },
  { path: '/albums',           view: './views/albums.js',        title: 'Album' },
  { path: '/albums/:id',       view: './views/album-detail.js',  title: 'Album' },
  { path: '/people',           view: './views/people.js',        title: 'Persone' },
  { path: '/places',           view: './views/places.js',        title: 'Luoghi' },
  { path: '/stats',            view: './views/stats.js',         title: 'Statistiche' },
  { path: '/index',            view: './views/organize.js',      title: 'Organizza' },
  { path: '/phone',            view: './views/phone.js',         title: 'Importa dal telefono' },
  { path: '/nas',              view: './views/nas.js',           title: 'NAS' },
  { path: '/settings',         view: './views/settings.js',      title: 'Impostazioni' },   // ?tab=general|network|labels|suggested
  { path: '/onboarding',       view: './views/onboarding.js',    title: 'Benvenuto' },
  { path: '/help',             view: './views/help.js',          title: 'Aiuto' },
];
