// PhotoScan site: theme toggle, mobile nav drawer, interactive screenshot tabs, video control, and touch lightbox.
(() => {
  const THEME_KEY = 'photoscan.site.theme';
  const root = document.documentElement;
  const isIt = (root.lang || '').toLowerCase().startsWith('it');
  const mqDark = window.matchMedia('(prefers-color-scheme: dark)');

  function isDark() {
    const saved = root.getAttribute('data-theme');
    if (saved === 'dark') return true;
    if (saved === 'light') return false;
    return mqDark.matches;
  }

  function syncThemeColor() {
    const color = isDark() ? '#111114' : '#fafaf9';
    let meta = document.querySelector('meta[name="theme-color"]:not([media])');
    if (!meta) {
      meta = document.createElement('meta');
      meta.name = 'theme-color';
      document.head.append(meta);
    }
    meta.content = color;
  }

  function syncThemeBtn() {
    const btn = document.getElementById('theme-toggle');
    if (!btn) return;
    const dark = isDark();
    btn.setAttribute('aria-pressed', String(dark));
    const sun = btn.querySelector('.icon-sun');
    const moon = btn.querySelector('.icon-moon');
    if (sun) sun.style.display = dark ? 'block' : 'none';
    if (moon) moon.style.display = dark ? 'none' : 'block';
  }

  try {
    const saved = localStorage.getItem(THEME_KEY);
    if (saved === 'light' || saved === 'dark') root.setAttribute('data-theme', saved);
  } catch { /* ignore */ }

  syncThemeBtn();
  syncThemeColor();
  mqDark.addEventListener('change', () => { syncThemeBtn(); syncThemeColor(); });

  document.getElementById('theme-toggle')?.addEventListener('click', () => {
    const next = isDark() ? 'light' : 'dark';
    root.setAttribute('data-theme', next);
    try { localStorage.setItem(THEME_KEY, next); } catch { /* ignore */ }
    syncThemeBtn();
    syncThemeColor();
  });

  // Mobile navigation drawer
  const menuBtn = document.getElementById('menu-toggle');
  const mobileMenu = document.getElementById('mobile-menu');
  if (menuBtn && mobileMenu) {
    const icMenu = menuBtn.querySelector('.icon-menu');
    const icClose = menuBtn.querySelector('.icon-close');
    const setMenuOpen = (open) => {
      mobileMenu.hidden = !open;
      menuBtn.setAttribute('aria-expanded', String(open));
      menuBtn.setAttribute(
        'aria-label',
        open
          ? (isIt ? 'Chiudi menu di navigazione' : 'Close navigation menu')
          : (isIt ? 'Apri menu di navigazione' : 'Open navigation menu'),
      );
      if (icMenu) icMenu.style.display = open ? 'none' : 'block';
      if (icClose) icClose.style.display = open ? 'block' : 'none';
    };

    menuBtn.addEventListener('click', () => setMenuOpen(mobileMenu.hidden));
    mobileMenu.querySelectorAll('a').forEach((link) => {
      link.addEventListener('click', () => setMenuOpen(false));
    });
    document.addEventListener('click', (e) => {
      if (!mobileMenu.hidden && !e.target.closest('.nav')) setMenuOpen(false);
    });
    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape' && !mobileMenu.hidden) {
        setMenuOpen(false);
        menuBtn.focus();
      }
    });
    window.matchMedia('(min-width: 761px)').addEventListener('change', (e) => {
      if (e.matches && !mobileMenu.hidden) setMenuOpen(false);
    });
  }

  // Hero video play/pause + prefers-reduced-motion
  document.querySelectorAll('[data-video-toggle]').forEach((btn) => {
    const video = btn.closest('.hero__media')?.querySelector('video');
    if (!video) return;
    const icPause = btn.querySelector('.ic-pause');
    const icPlay = btn.querySelector('.ic-play');
    const sync = () => {
      const paused = video.paused;
      if (icPause) icPause.style.display = paused ? 'none' : 'block';
      if (icPlay) icPlay.style.display = paused ? 'block' : 'none';
      btn.setAttribute('aria-pressed', String(!paused));
    };
    if (window.matchMedia('(prefers-reduced-motion: reduce)').matches) video.pause();
    video.addEventListener('play', sync);
    video.addEventListener('pause', sync);
    btn.addEventListener('click', () => {
      if (video.paused) video.play().catch(() => {});
      else video.pause();
    });
    sync();
  });

  // OS download switcher (Windows / Linux) with automatic OS pre-selection
  const osTabs = [...document.querySelectorAll('[data-os-tab]')];
  const osPanels = [...document.querySelectorAll('[data-os-panel]')];
  if (osTabs.length && osPanels.length) {
    const selectOs = (os) => {
      osTabs.forEach((btn) => {
        const on = btn.dataset.osTab === os;
        btn.classList.toggle('is-active', on);
        btn.setAttribute('aria-selected', String(on));
      });
      osPanels.forEach((panel) => {
        panel.hidden = panel.dataset.osPanel !== os;
      });
    };
    osTabs.forEach((btn) => {
      btn.addEventListener('click', () => selectOs(btn.dataset.osTab));
    });
    const ua = `${navigator.userAgent || ''} ${navigator.platform || ''}`.toLowerCase();
    if (ua.includes('linux') && !ua.includes('android')) {
      selectOs('linux');
    }
  }

  // Interactive screenshot showcase + lightbox
  const shots = [...document.querySelectorAll('.stage__data span')].map((el) => ({
    src: el.dataset.src,
    alt: el.dataset.alt || '',
    caption: el.dataset.caption || '',
    mobile: el.dataset.mobile === 'true',
  }));
  if (!shots.length) return;

  const tabs = [...document.querySelectorAll('[data-shot-tab]')];
  const stageTrigger = document.getElementById('stage-trigger');
  const stageImg = document.getElementById('stage-img');
  const stageCap = document.getElementById('stage-caption');
  let active = 0;

  function selectShot(i, scrollTab = false) {
    active = (i + shots.length) % shots.length;
    const s = shots[active];
    if (stageImg) {
      stageImg.src = s.src;
      stageImg.alt = s.alt;
    }
    if (stageCap) stageCap.textContent = s.caption;
    if (stageTrigger) stageTrigger.classList.toggle('is-mobile-shot', s.mobile);
    tabs.forEach((t, idx) => {
      const on = idx === active;
      t.classList.toggle('is-active', on);
      t.setAttribute('aria-selected', String(on));
      if (on && scrollTab) t.scrollIntoView({ behavior: 'smooth', inline: 'center', block: 'nearest' });
    });
  }

  tabs.forEach((tab) => {
    tab.addEventListener('click', () => selectShot(Number(tab.dataset.shotTab)));
  });
  document.getElementById('stage-prev')?.addEventListener('click', () => selectShot(active - 1, true));
  document.getElementById('stage-next')?.addEventListener('click', () => selectShot(active + 1, true));

  // Touch swipe on the stage itself
  if (stageTrigger) {
    let sx = 0;
    let sy = 0;
    stageTrigger.addEventListener('touchstart', (e) => {
      if (e.touches.length !== 1) return;
      sx = e.touches[0].clientX;
      sy = e.touches[0].clientY;
    }, { passive: true });
    stageTrigger.addEventListener('touchend', (e) => {
      if (e.changedTouches.length !== 1) return;
      const dx = e.changedTouches[0].clientX - sx;
      const dy = e.changedTouches[0].clientY - sy;
      if (Math.abs(dx) > 45 && Math.abs(dx) > Math.abs(dy) * 1.2) {
        selectShot(dx > 0 ? active - 1 : active + 1, true);
      }
    }, { passive: true });
  }

  // Lightbox
  const lb = document.createElement('div');
  lb.className = 'lightbox';
  lb.setAttribute('role', 'dialog');
  lb.setAttribute('aria-modal', 'true');
  lb.setAttribute('aria-hidden', 'true');
  lb.innerHTML = `
    <button type="button" class="lightbox__btn lightbox__close" aria-label="${isIt ? 'Chiudi' : 'Close'}">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="20" height="20"><path d="M18 6L6 18M6 6l12 12"/></svg>
    </button>
    <button type="button" class="lightbox__btn lightbox__prev" aria-label="${isIt ? 'Precedente' : 'Previous'}">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="20" height="20"><path d="M15 18l-6-6 6-6"/></svg>
    </button>
    <img class="lightbox__img" alt="">
    <p class="lightbox__caption"></p>
    <div class="lightbox__counter" aria-live="polite"></div>
    <button type="button" class="lightbox__btn lightbox__next" aria-label="${isIt ? 'Successiva' : 'Next'}">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="20" height="20"><path d="M9 18l6-6-6-6"/></svg>
    </button>`;
  document.body.append(lb);

  const lbImg = lb.querySelector('.lightbox__img');
  const lbCap = lb.querySelector('.lightbox__caption');
  const lbCounter = lb.querySelector('.lightbox__counter');

  function openLightbox(i) {
    selectShot(i);
    const s = shots[active];
    lbImg.src = s.src;
    lbImg.alt = s.alt;
    lbCap.textContent = s.caption;
    lbCounter.textContent = `${active + 1} / ${shots.length}`;
    lb.classList.add('is-open');
    lb.setAttribute('aria-hidden', 'false');
    document.body.style.overflow = 'hidden';
  }

  function closeLightbox() {
    lb.classList.remove('is-open');
    lb.setAttribute('aria-hidden', 'true');
    document.body.style.overflow = '';
  }

  stageTrigger?.addEventListener('click', () => openLightbox(active));
  lb.addEventListener('click', (e) => { if (e.target === lb) closeLightbox(); });
  lb.querySelector('.lightbox__close').addEventListener('click', closeLightbox);
  lb.querySelector('.lightbox__prev').addEventListener('click', () => openLightbox(active - 1));
  lb.querySelector('.lightbox__next').addEventListener('click', () => openLightbox(active + 1));

  let lbX = 0;
  let lbY = 0;
  lb.addEventListener('touchstart', (e) => {
    if (e.touches.length !== 1) return;
    lbX = e.touches[0].clientX;
    lbY = e.touches[0].clientY;
  }, { passive: true });
  lb.addEventListener('touchend', (e) => {
    if (!lb.classList.contains('is-open') || e.changedTouches.length !== 1) return;
    const dx = e.changedTouches[0].clientX - lbX;
    const dy = e.changedTouches[0].clientY - lbY;
    if (Math.abs(dx) > 45 && Math.abs(dx) > Math.abs(dy) * 1.2) {
      openLightbox(dx > 0 ? active - 1 : active + 1);
    }
  }, { passive: true });

  document.addEventListener('keydown', (e) => {
    if (!lb.classList.contains('is-open')) return;
    if (e.key === 'Escape') closeLightbox();
    else if (e.key === 'ArrowLeft') openLightbox(active - 1);
    else if (e.key === 'ArrowRight') openLightbox(active + 1);
  });
})();
