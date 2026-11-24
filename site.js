// PhotoScan site enhancements: light/dark theme toggle (syncs hero preview), copy-to-clipboard, and screenshot lightbox.
(() => {
  const THEME_KEY = 'photoscan.site.theme';
  const root = document.documentElement;
  const mqDark = window.matchMedia('(prefers-color-scheme: dark)');

  function isDark() {
    const saved = root.getAttribute('data-theme');
    if (saved === 'dark') return true;
    if (saved === 'light') return false;
    return mqDark.matches;
  }

  function syncHeroImage() {
    const heroImg = document.querySelector('[data-hero-img]');
    if (!heroImg) return;
    const next = isDark() ? heroImg.dataset.darkSrc : heroImg.dataset.lightSrc;
    if (next && heroImg.getAttribute('src') !== next) heroImg.setAttribute('src', next);
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
  syncHeroImage();
  mqDark.addEventListener('change', () => { syncThemeBtn(); syncHeroImage(); });

  document.getElementById('theme-toggle')?.addEventListener('click', () => {
    const next = isDark() ? 'light' : 'dark';
    root.setAttribute('data-theme', next);
    try { localStorage.setItem(THEME_KEY, next); } catch { /* ignore */ }
    syncThemeBtn();
    syncHeroImage();
  });

  // Copy command button
  document.querySelectorAll('[data-copy-target]').forEach((btn) => {
    btn.addEventListener('click', async () => {
      const target = document.getElementById(btn.dataset.copyTarget);
      if (!target) return;
      try {
        await navigator.clipboard.writeText(target.textContent.trim());
        const label = btn.querySelector('span');
        const orig = label?.textContent || '';
        if (label) label.textContent = btn.dataset.copiedLabel || 'Copied!';
        btn.classList.add('is-copied');
        setTimeout(() => {
          if (label) label.textContent = orig;
          btn.classList.remove('is-copied');
        }, 1800);
      } catch { /* ignore */ }
    });
  });

  // Hero video play/pause + prefers-reduced-motion support
  document.querySelectorAll('[data-video-toggle]').forEach((btn) => {
    const video = btn.closest('.window-frame__media')?.querySelector('video');
    if (!video) return;
    const icPause = btn.querySelector('.ic-pause');
    const icPlay = btn.querySelector('.ic-play');
    const sync = () => {
      const paused = video.paused;
      if (icPause) icPause.style.display = paused ? 'none' : 'block';
      if (icPlay) icPlay.style.display = paused ? 'block' : 'none';
      btn.setAttribute('aria-pressed', String(!paused));
    };
    if (window.matchMedia('(prefers-reduced-motion: reduce)').matches) {
      video.pause();
    }
    video.addEventListener('play', sync);
    video.addEventListener('pause', sync);
    btn.addEventListener('click', () => {
      if (video.paused) video.play().catch(() => {});
      else video.pause();
    });
    sync();
  });

  // Lightbox for screenshots + hero
  const triggers = [...document.querySelectorAll('[data-lightbox]')];
  if (!triggers.length) return;

  const lb = document.createElement('div');
  lb.className = 'lightbox';
  lb.setAttribute('role', 'dialog');
  lb.setAttribute('aria-modal', 'true');
  lb.setAttribute('aria-hidden', 'true');
  lb.innerHTML = `
    <button type="button" class="lightbox__btn lightbox__close" aria-label="Close">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="20" height="20"><path d="M18 6L6 18M6 6l12 12"/></svg>
    </button>
    <button type="button" class="lightbox__btn lightbox__prev" aria-label="Previous">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="20" height="20"><path d="M15 18l-6-6 6-6"/></svg>
    </button>
    <img class="lightbox__img" alt="">
    <p class="lightbox__caption"></p>
    <button type="button" class="lightbox__btn lightbox__next" aria-label="Next">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="20" height="20"><path d="M9 18l6-6-6-6"/></svg>
    </button>`;
  document.body.append(lb);

  const lbImg = lb.querySelector('.lightbox__img');
  const lbCap = lb.querySelector('.lightbox__caption');
  let cur = 0;

  function show(i) {
    cur = (i + triggers.length) % triggers.length;
    const el = triggers[cur];
    const img = el.tagName === 'IMG' ? el : el.querySelector('img');
    lbImg.src = img.currentSrc || img.src;
    lbImg.alt = img.alt || '';
    lbCap.textContent = el.dataset.caption || img.alt || '';
    lb.classList.add('is-open');
    lb.setAttribute('aria-hidden', 'false');
    document.body.style.overflow = 'hidden';
  }

  function close() {
    lb.classList.remove('is-open');
    lb.setAttribute('aria-hidden', 'true');
    document.body.style.overflow = '';
  }

  triggers.forEach((el, i) => {
    el.addEventListener('click', () => show(i));
  });
  lb.addEventListener('click', (e) => { if (e.target === lb) close(); });
  lb.querySelector('.lightbox__close').addEventListener('click', close);
  lb.querySelector('.lightbox__prev').addEventListener('click', () => show(cur - 1));
  lb.querySelector('.lightbox__next').addEventListener('click', () => show(cur + 1));
  document.addEventListener('keydown', (e) => {
    if (!lb.classList.contains('is-open')) return;
    if (e.key === 'Escape') close();
    else if (e.key === 'ArrowLeft') show(cur - 1);
    else if (e.key === 'ArrowRight') show(cur + 1);
  });
})();
