"""Screenshot di una pagina con Edge (Playwright, nessun download di browser).

  python tools/shot.py --url "http://127.0.0.1:9101/#/gallery" --out C:\\...\\gallery.png
        [--width 1280 --height 800] [--dark] [--wait 1500] [--full]
        [--click "css-selector"] [--eval "js"] [--scroll 800] [--text "selector"]

Stampa errori della console, richieste fallite e (con --text) il testo dell'elemento. Poi LEGGI l'immagine con lo strumento Read
per verificare davvero l'aspetto.   --width 390 --height 844 = telefono (layout compatto), 820 = medio, 1280+ = esteso.
"""
from __future__ import annotations

import argparse
import sys

from playwright.sync_api import sync_playwright


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--width", type=int, default=1280)
    ap.add_argument("--height", type=int, default=800)
    ap.add_argument("--dark", action="store_true")
    ap.add_argument("--wait", type=int, default=1500, help="ms di attesa dopo il caricamento")
    ap.add_argument("--full", action="store_true", help="screenshot dell'intera pagina scrollabile")
    ap.add_argument("--click", action="append", default=[], help="selettore CSS da cliccare (ripetibile, in ordine)")
    ap.add_argument("--eval", dest="js", default=None, help="JavaScript da eseguire dopo il caricamento")
    ap.add_argument("--scroll", type=int, default=0, help="px di scroll del contenitore principale")
    ap.add_argument("--text", default=None, help="stampa innerText del primo elemento che combacia")
    a = ap.parse_args()
    errors: list[str] = []
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="msedge", headless=True)
        ctx = browser.new_context(viewport={"width": a.width, "height": a.height},
                                  color_scheme="dark" if a.dark else "light", device_scale_factor=1)
        page = ctx.new_page()
        page.on("console", lambda m: errors.append(f"[console.{m.type}] {m.text}") if m.type in ("error", "warning") else None)
        page.on("pageerror", lambda e: errors.append(f"[pageerror] {e}"))
        page.on("requestfailed", lambda r: errors.append(f"[requestfailed] {r.url} {r.failure}"))
        page.on("response", lambda r: errors.append(f"[http {r.status}] {r.url}") if r.status >= 400 else None)
        page.goto(a.url, wait_until="load")
        page.wait_for_timeout(a.wait)
        for sel in a.click:
            page.click(sel, timeout=5000)
            page.wait_for_timeout(500)
        if a.js:
            r = page.evaluate(a.js)
            print("eval ->", r)
            page.wait_for_timeout(400)
        if a.scroll:
            page.evaluate("(y) => { const o = document.querySelector('.view-outlet, main, #view') || document.scrollingElement; o.scrollTop = y; }", a.scroll)
            page.wait_for_timeout(600)
        if a.text:
            el = page.query_selector(a.text)
            print("text ->", el.inner_text() if el else "(non trovato)")
        page.screenshot(path=a.out, full_page=a.full)
        browser.close()
    print("screenshot:", a.out)
    for e in dict.fromkeys(errors):
        print(e)
    return 0


if __name__ == "__main__":
    sys.exit(main())
