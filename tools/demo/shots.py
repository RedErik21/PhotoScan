"""Screenshot per GitHub dalla libreria demo (server su :9300). Uso: python shots.py OUTDIR [solo_nome ...]"""
import sys
from pathlib import Path
from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:9300/"
OUT = Path(sys.argv[1]); OUT.mkdir(parents=True, exist_ok=True)
ONLY = set(sys.argv[2:])
W, H = 1600, 1000

# nome, hash, tema, dimensione, azioni (funzione page -> None)
def search(q):
    def go(p):
        inp = p.locator("input[type=search], md-outlined-text-field, .search-field input").first
        p.evaluate("""(q) => {
          const tf = document.querySelector('.view md-outlined-text-field, .view input[type=search], md-filled-text-field');
          const el = tf && (tf.shadowRoot?.querySelector('input') || tf);
          if (tf) { tf.value = q; tf.dispatchEvent(new Event('input', {bubbles: true})); tf.dispatchEvent(new KeyboardEvent('keydown', {key: 'Enter', bubbles: true})); }
        }""", q)
        p.wait_for_timeout(3500)
    return go

def to_year(y):
    def go(p):
        # scorre fino al primo giorno dell'anno y (galleria virtualizzata: rotella finche' compare)
        p.mouse.move(p.viewport_size["width"] * 0.6, p.viewport_size["height"] * 0.6)
        for _ in range(200):
            hdr = p.get_by_text(f"agosto {y}").first if y == 2024 else p.get_by_text(f"febbraio {y}").first
            if hdr.count() and hdr.is_visible():
                hdr.evaluate("el => el.scrollIntoView({block: 'start'})"); p.wait_for_timeout(300); p.mouse.wheel(0, -20)
                break
            p.mouse.wheel(0, 900); p.wait_for_timeout(120)
        p.wait_for_timeout(2500)
    return go

def click_text(t, scroll=0):
    def go(p):
        p.get_by_text(t, exact=True).first.click()
        p.wait_for_timeout(2500)
        if scroll:
            p.mouse.move(1100, 700); p.mouse.wheel(0, scroll); p.wait_for_timeout(1500)
    return go

def wheel(n):
    def go(p):
        p.mouse.move(1100, 700); p.mouse.wheel(0, n); p.wait_for_timeout(2000)
    return go

def open_first_photo(p):
    p.locator(".photo-tile").nth(5).click()
    p.wait_for_timeout(2000)

SHOTS = [
    ("01-home", "#/", "light", (W, H), None),
    ("02-galleria", "#/gallery", "light", (W, H), to_year(2024)),
    ("03-cerca", "#/search?q=tramonto", "light", (W, H), None),
    ("04-cerca-neve", "#/search?q=neve", "dark", (W, H), None),
    ("05-visualizzatore", "#/gallery", "dark", (W, H), open_first_photo),
    ("06-luoghi", "#/places", "light", (W, H), None),
    ("07-album", "#/albums", "light", (W, H), None),
    ("08-analisi", "#/organize", "light", (W, H), click_text("Analisi")),
    ("09-duplicati", "#/duplicates", "light", (W, H), wheel(950)),
    ("10-libera-spazio", "#/cleanup", "light", (W, H), click_text("Screenshot", 520)),
    ("11-statistiche", "#/stats", "light", (W, H), None),
    ("12-impostazioni", "#/settings", "light", (W, H), None),
    ("13-home-scuro", "#/", "dark", (W, H), None),
    ("14-telefono-galleria", "#/gallery", "light", (390, 844), to_year(2023)),
    ("15-telefono-cerca", "#/search?q=citt%C3%A0%20di%20notte", "dark", (390, 844), None),
]

with sync_playwright() as pw:
    br = pw.chromium.launch(channel="msedge", headless=True)
    for name, hsh, theme, (w, h), act in SHOTS:
        if ONLY and name not in ONLY:
            continue
        mobile = w < 600
        ctx = br.new_context(viewport={"width": w, "height": h}, color_scheme=theme, device_scale_factor=3 if mobile else 1.5,
                             is_mobile=mobile, has_touch=mobile)
        ctx.add_init_script(f"localStorage.setItem('fotoscan.theme', '{theme}');")
        page = ctx.new_page()
        page.goto(BASE + hsh, wait_until="load")
        page.wait_for_timeout(3500)
        if act:
            act(page)
        page.wait_for_timeout(800)
        page.screenshot(path=str(OUT / f"{name}.png"))
        print("ok", name, flush=True)
        ctx.close()
    br.close()
