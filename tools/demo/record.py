"""Video promozionale: tour guidato dell'app (libreria demo su :9300) registrato con lo screencast di Chromium.

Uso: python record.py OUT_DIR   -> OUT_DIR/frames/*.jpg + OUT_DIR/frames.txt (concat ffmpeg con durate reali)
"""
import base64, json, sys, time
from pathlib import Path
from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:9300/"
OUT = Path(sys.argv[1]); FR = OUT / "frames"; FR.mkdir(parents=True, exist_ok=True)
for f in FR.glob("*.jpg"):
    f.unlink()
ICON = base64.b64encode((Path(__file__).resolve().parents[2] / "frontend" / "icon-512.png").read_bytes()).decode()

OVERLAY_JS = """
(icon) => {
  if (document.getElementById('promo-style')) return;
  const st = document.createElement('style'); st.id = 'promo-style';
  st.textContent = `
  #promo-cap { position: fixed; left: 50%; bottom: 44px; transform: translate(-50%, 20px); z-index: 99999;
    padding: 16px 30px; border-radius: 999px; font: 600 26px/1.25 'Segoe UI Variable Display', 'Segoe UI', sans-serif;
    color: #fff; background: rgba(28, 22, 58, .82); backdrop-filter: blur(14px); box-shadow: 0 10px 40px rgba(0,0,0,.25);
    opacity: 0; transition: opacity .45s ease, transform .45s ease; white-space: nowrap; letter-spacing: .2px; }
  #promo-cap.on { opacity: 1; transform: translate(-50%, 0); }
  #promo-card { position: fixed; inset: 0; z-index: 100000; display: flex; flex-direction: column; align-items: center;
    justify-content: center; gap: 22px; color: #fff; text-align: center; opacity: 0; pointer-events: none;
    background: radial-gradient(1200px 700px at 30% 20%, #7b5cff 0%, transparent 60%),
                radial-gradient(900px 600px at 80% 90%, #ff7a9a 0%, transparent 55%), #2a1d73;
    transition: opacity .7s ease; font-family: 'Segoe UI Variable Display', 'Segoe UI', sans-serif; }
  #promo-card.on { opacity: 1; }
  #promo-card img { width: 150px; height: 150px; border-radius: 36px; box-shadow: 0 20px 60px rgba(0,0,0,.35); }
  #promo-card h1 { margin: 0 !important; font-size: 92px !important; line-height: 1.15 !important; height: auto !important; font-weight: 700; letter-spacing: -1px; color: #fff; }
  #promo-card p { margin: 0 !important; line-height: 1.3 !important; font-size: 34px; font-weight: 400; opacity: .92; max-width: 1300px; }
  #promo-card .chips { display: flex; gap: 16px; margin-top: 18px; }
  #promo-card .chips span { padding: 10px 24px; border-radius: 999px; background: rgba(255,255,255,.16); font-size: 24px; }
  #promo-cursor { position: fixed; left: 0; top: 0; width: 26px; height: 26px; margin: -13px 0 0 -13px; border-radius: 50%;
    z-index: 99998; pointer-events: none; background: rgba(255,255,255,.95); border: 3px solid #4f3cd6;
    box-shadow: 0 4px 14px rgba(0,0,0,.35); transition: left .7s cubic-bezier(.4,0,.2,1), top .7s cubic-bezier(.4,0,.2,1), transform .15s; opacity: 0; }
  #promo-cursor.on { opacity: 1; }
  #promo-cursor.down { transform: scale(.7); }`;
  document.head.append(st);
  const cap = document.createElement('div'); cap.id = 'promo-cap'; document.body.append(cap);
  const card = document.createElement('div'); card.id = 'promo-card'; document.body.append(card);
  const cur = document.createElement('div'); cur.id = 'promo-cursor'; document.body.append(cur);
  window.__promoIcon = 'data:image/png;base64,' + icon;
}
"""

frames = []  # (timestamp, path)


def main():
    with sync_playwright() as pw:
        br = pw.chromium.launch(channel="msedge", headless=True)
        ctx = br.new_context(viewport={"width": 1920, "height": 1080}, device_scale_factor=1, color_scheme="light")
        ctx.add_init_script("localStorage.setItem('fotoscan.theme', 'light');")
        page = ctx.new_page()
        page.goto(BASE + "#/", wait_until="load")
        page.wait_for_timeout(3000)

        def ensure():
            page.evaluate(OVERLAY_JS, ICON)

        def caption(text):
            ensure()
            page.evaluate("""(t) => { const c = document.getElementById('promo-cap');
              c.classList.remove('on'); setTimeout(() => { c.textContent = t; if (t) c.classList.add('on'); }, t ? 250 : 0); }""", text)

        def card(title, sub, chips=None, on=True):
            ensure()
            page.evaluate("""([t, s, chips, on]) => { const c = document.getElementById('promo-card');
              if (on) c.innerHTML = `<img src="${window.__promoIcon}"><h1>${t}</h1><p>${s}</p>` +
                 (chips ? `<div class="chips">${chips.map((x) => `<span>${x}</span>`).join('')}</div>` : '');
              c.classList.toggle('on', on); }""", [title, sub, chips, on])

        def cursor_to(x, y):
            ensure()
            page.evaluate("([x, y]) => { const c = document.getElementById('promo-cursor'); c.classList.add('on'); c.style.left = x + 'px'; c.style.top = y + 'px'; }", [x, y])
            page.wait_for_timeout(800)

        def click(loc):
            loc.scroll_into_view_if_needed()
            b = loc.bounding_box()
            x, y = b["x"] + b["width"] / 2, b["y"] + b["height"] / 2
            cursor_to(x, y)
            page.evaluate("() => { const c = document.getElementById('promo-cursor'); c.classList.add('down'); setTimeout(() => c.classList.remove('down'), 180); }")
            page.mouse.click(x, y)

        def smooth_scroll(total, steps=60, delay=28, x=1150, y=650):
            page.mouse.move(x, y)
            for _ in range(steps):
                page.mouse.wheel(0, total / steps)
                page.wait_for_timeout(delay)

        def pause():
            cdp.send("Page.stopScreencast"); page.wait_for_timeout(200); frames.append((time.time(), None))

        def resume():
            cdp.send("Page.startScreencast", {"format": "jpeg", "quality": 92, "maxWidth": 1920, "maxHeight": 1080, "everyNthFrame": 1})
            page.wait_for_timeout(150)

        def scroll_until(text, max_steps=300):
            page.mouse.move(1150, 650)
            for _ in range(max_steps):
                hdr = page.get_by_text(text).first
                if hdr.count() and hdr.is_visible():
                    hdr.evaluate("el => el.scrollIntoView({block: 'start'})"); page.wait_for_timeout(400); page.mouse.wheel(0, -20)
                    break
                page.mouse.wheel(0, 900); page.wait_for_timeout(80)
            page.wait_for_timeout(1500)

        def go(hsh, wait=2200):
            page.evaluate("(h) => { location.hash = h; }", hsh)
            page.wait_for_timeout(wait)

        # ---------------- registrazione
        cdp = ctx.new_cdp_session(page)
        n = [0]

        def on_frame(ev):
            n[0] += 1
            p = FR / f"{n[0]:06d}.jpg"
            p.write_bytes(base64.b64decode(ev["data"]))
            frames.append((ev["metadata"]["timestamp"], p))
            try:
                cdp.send("Page.screencastFrameAck", {"sessionId": ev["sessionId"]})
            except Exception:
                pass

        cdp.on("Page.screencastFrame", on_frame)
        ensure()
        card("PhotoScan", "Il tuo archivio di foto, organizzato dall’intelligenza artificiale.",
             ["Ricerca per parole", "Cartelle automatiche", "100% sul tuo computer"])
        page.wait_for_timeout(300)
        cdp.send("Page.startScreencast", {"format": "jpeg", "quality": 92, "maxWidth": 1920, "maxHeight": 1080, "everyNthFrame": 1})
        page.wait_for_timeout(3800)

        # 1. Home
        card("", "", on=False)
        page.wait_for_timeout(700)
        caption("Tutte le tue foto e i video, in un unico posto")
        page.wait_for_timeout(3000)

        # 2. Galleria
        click(page.locator("a.nav-link, a").filter(has_text="Foto e video").first)
        page.wait_for_timeout(1500)
        pause(); scroll_until("13 agosto 2024"); resume()
        caption("Sfoglia l’archivio per giorni, mesi e anni")
        smooth_scroll(2300, steps=80, delay=32)
        page.wait_for_timeout(1200)

        # 3. Ricerca
        caption("Cerca con parole tue: l’AI capisce cosa c’è nelle foto")
        click(page.locator(".topbar-search-input"))
        page.keyboard.type("tramonto", delay=110)
        page.keyboard.press("Enter")
        page.wait_for_timeout(3600)
        smooth_scroll(600, steps=30)
        page.wait_for_timeout(1200)
        field = page.locator(".search-input")
        click(field)
        field.fill("")
        page.keyboard.type("città di notte", delay=100)
        page.wait_for_timeout(3600)

        # 4. Visualizzatore
        caption("Apri una foto a schermo intero, con tutti i dettagli")
        click(page.locator(".photo-tile").nth(1))
        page.wait_for_timeout(2200)
        page.keyboard.press("ArrowRight"); page.wait_for_timeout(1500)
        page.keyboard.press("ArrowRight"); page.wait_for_timeout(1500)
        page.keyboard.press("Escape"); page.wait_for_timeout(900)

        # 5. Luoghi
        caption("Rivivi i tuoi viaggi sulla mappa")
        click(page.locator("a").filter(has_text="Luoghi").first)
        page.wait_for_timeout(3800)
        smooth_scroll(520, steps=30)
        page.wait_for_timeout(1500)

        # 6. Album
        caption("Album suggeriti, creati dai temi ricorrenti delle tue foto")
        click(page.locator("a").filter(has_text="Album").first)
        page.wait_for_timeout(2000)
        smooth_scroll(620, steps=30)
        page.wait_for_timeout(2600)

        # 7. Organizza / Libera spazio
        caption("Trova duplicati, video pesanti e screenshot: libera spazio in sicurezza")
        click(page.locator("a").filter(has_text="Organizza").first)
        page.wait_for_timeout(2500)
        click(page.get_by_text("Screenshot", exact=True).first)
        page.wait_for_timeout(1500)
        smooth_scroll(560, steps=30)
        page.wait_for_timeout(2400)

        # 8. Statistiche
        caption("Statistiche del tuo archivio, anno per anno")
        click(page.locator("a").filter(has_text="Statistiche").first)
        page.wait_for_timeout(3500)

        # 9. Tema scuro
        caption("Tema chiaro e scuro")
        click(page.locator(".nav-theme-btn").first)
        page.wait_for_timeout(600)
        click(page.locator("a").filter(has_text="Home").first)
        page.wait_for_timeout(1200)
        click(page.locator("a").filter(has_text="Foto e video").first)
        page.wait_for_timeout(1200)
        pause(); scroll_until("febbraio 2023"); resume()
        page.wait_for_timeout(800)
        smooth_scroll(1300, steps=50, delay=32)
        page.wait_for_timeout(1200)

        # 10. Chiusura
        caption("")
        page.evaluate("() => document.getElementById('promo-cursor').classList.remove('on')")
        card("PhotoScan", "Le tue foto restano tue: tutto avviene sul tuo PC, nessun cloud.",
             ["Gratuito", "Open source", "Windows · GPU NVIDIA o processore"])
        page.wait_for_timeout(4500)
        end_ts = time.time()
        cdp.send("Page.stopScreencast")
        page.wait_for_timeout(300)
        br.close()

    # fotogrammi solo quando lo schermo cambia: ognuno dura fino al successivo; prima di una pausa 1/30 s
    seq = frames + [(end_ts, "END")]
    total, last = 0.0, None
    with open(OUT / "frames.txt", "w", encoding="utf-8") as f:
        for (ts, p), (nts, np_) in zip(seq, seq[1:]):
            if p is None:
                continue
            d = 1 / 30 if np_ is None else max(nts - ts, 0.001)
            total += d; last = p
            f.write(f"file '{p.as_posix()}'\nduration {d:.4f}\n")
        f.write(f"file '{last.as_posix()}'\n")
    print("fotogrammi", len([x for x in frames if x[1]]), "durata %.1fs" % total)


main()
