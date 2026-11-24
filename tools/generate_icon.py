"""Generatore professionale dell'icona Material 3 di PhotoScan in SVG, ICO e PNG multi-risoluzione."""
import asyncio
import ctypes
from pathlib import Path
from PIL import Image
from playwright.async_api import async_playwright

SVG_CONTENT = '''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512" width="100%" height="100%">
  <defs>
    <!-- Ombreggiatura squircle base con caduta morbida M3 -->
    <filter id="m3-shadow" x="-20%" y="-20%" width="140%" height="140%">
      <feDropShadow dx="0" dy="16" stdDeviation="20" flood-color="#0a192f" flood-opacity="0.32"/>
      <feDropShadow dx="0" dy="4" stdDeviation="6" flood-color="#0a192f" flood-opacity="0.18"/>
    </filter>

    <!-- Ombreggiatura elementi in primo piano -->
    <filter id="card-shadow" x="-25%" y="-25%" width="150%" height="150%">
      <feDropShadow dx="0" dy="8" stdDeviation="12" flood-color="#071938" flood-opacity="0.28"/>
      <feDropShadow dx="0" dy="2" stdDeviation="4" flood-color="#071938" flood-opacity="0.16"/>
    </filter>

    <!-- Bagliore Lente & Laser AI -->
    <filter id="scan-glow" x="-30%" y="-30%" width="160%" height="160%">
      <feDropShadow dx="0" dy="0" stdDeviation="8" flood-color="#38bdf8" flood-opacity="0.6"/>
    </filter>

    <!-- Bagliore Stella AI Auto-Awesome -->
    <filter id="star-glow" x="-40%" y="-40%" width="180%" height="180%">
      <feDropShadow dx="0" dy="2" stdDeviation="6" flood-color="#d97706" flood-opacity="0.4"/>
      <feDropShadow dx="0" dy="0" stdDeviation="10" flood-color="#fbbf24" flood-opacity="0.35"/>
    </filter>

    <!-- Gradiente superficie Squircle M3 (Blu Google / Cobalto profondo a Celeste luminoso) -->
    <linearGradient id="bg-grad" x1="10%" y1="0%" x2="90%" y2="100%">
      <stop offset="0%" stop-color="#1d4ed8"/>
      <stop offset="45%" stop-color="#2563eb"/>
      <stop offset="100%" stop-color="#0284c7"/>
    </linearGradient>

    <!-- Riflesso bordo superiore (High-specular Material rim) -->
    <linearGradient id="rim-grad" x1="0%" y1="0%" x2="0%" y2="100%">
      <stop offset="0%" stop-color="#ffffff" stop-opacity="0.5"/>
      <stop offset="25%" stop-color="#ffffff" stop-opacity="0.15"/>
      <stop offset="100%" stop-color="#ffffff" stop-opacity="0.0"/>
    </linearGradient>

    <!-- Superficie Camera M3 (Bianco puro a Slate chiarissimo) -->
    <linearGradient id="cam-surface" x1="0%" y1="0%" x2="0%" y2="100%">
      <stop offset="0%" stop-color="#ffffff"/>
      <stop offset="100%" stop-color="#f8fafc"/>
    </linearGradient>

    <!-- Ghiera metallica esterna lente -->
    <linearGradient id="bezel-ring" x1="0%" y1="0%" x2="100%" y2="100%">
      <stop offset="0%" stop-color="#334155"/>
      <stop offset="50%" stop-color="#1e293b"/>
      <stop offset="100%" stop-color="#0f172a"/>
    </linearGradient>

    <!-- Centro lente in vetro profondo con riflesso ottico -->
    <radialGradient id="lens-glass" cx="38%" cy="36%" r="65%">
      <stop offset="0%" stop-color="#38bdf8"/>
      <stop offset="30%" stop-color="#0284c7"/>
      <stop offset="70%" stop-color="#0f172a"/>
      <stop offset="100%" stop-color="#020617"/>
    </radialGradient>

    <!-- Raggio di scansione AI (Scan line) -->
    <linearGradient id="beam-grad" x1="0%" y1="0%" x2="100%" y2="0%">
      <stop offset="0%" stop-color="#38bdf8" stop-opacity="0"/>
      <stop offset="25%" stop-color="#38bdf8" stop-opacity="0.8"/>
      <stop offset="50%" stop-color="#ffffff" stop-opacity="1"/>
      <stop offset="75%" stop-color="#38bdf8" stop-opacity="0.8"/>
      <stop offset="100%" stop-color="#38bdf8" stop-opacity="0"/>
    </linearGradient>

    <!-- Colori dei petali del diaframma (Palette Material Design 3) -->
    <linearGradient id="iris-blue" x1="0%" y1="0%" x2="100%" y2="100%">
      <stop offset="0%" stop-color="#60a5fa"/>
      <stop offset="100%" stop-color="#2563eb"/>
    </linearGradient>
    <linearGradient id="iris-teal" x1="0%" y1="0%" x2="100%" y2="100%">
      <stop offset="0%" stop-color="#34d399"/>
      <stop offset="100%" stop-color="#059669"/>
    </linearGradient>
    <linearGradient id="iris-amber" x1="0%" y1="0%" x2="100%" y2="100%">
      <stop offset="0%" stop-color="#fbbf24"/>
      <stop offset="100%" stop-color="#d97706"/>
    </linearGradient>
    <linearGradient id="iris-coral" x1="0%" y1="0%" x2="100%" y2="100%">
      <stop offset="0%" stop-color="#fb7185"/>
      <stop offset="100%" stop-color="#e11d48"/>
    </linearGradient>

    <!-- Stella AI Auto-Awesome (Oro/Ambra Material) -->
    <linearGradient id="star-gold" x1="0%" y1="0%" x2="100%" y2="100%">
      <stop offset="0%" stop-color="#fef08a"/>
      <stop offset="45%" stop-color="#fbbf24"/>
      <stop offset="100%" stop-color="#f59e0b"/>
    </linearGradient>
  </defs>

  <!-- 1. Squircle Base M3 (416x416 centrato a 256, con raggio 96) -->
  <rect x="48" y="48" width="416" height="416" rx="96" ry="96" fill="url(#bg-grad)" filter="url(#m3-shadow)"/>
  
  <!-- 2. Bordo satinato superiore per effetto tridimensionale M3 -->
  <rect x="50" y="50" width="412" height="412" rx="94" ry="94" fill="none" stroke="url(#rim-grad)" stroke-width="2.5"/>

  <!-- 3. Foto card inclinata sullo sfondo (rappresenta l'organizzazione dell'archivio) -->
  <g transform="rotate(-6 256 266)" opacity="0.35">
    <rect x="134" y="148" width="244" height="204" rx="28" fill="#ffffff" filter="url(#card-shadow)"/>
    <!-- Mini silhouette orizzonte/montagna nella card di sfondo -->
    <path d="M 144 316 L 206 250 L 254 290 L 302 240 L 368 316 Z" fill="#93c5fd" opacity="0.45"/>
  </g>

  <!-- 4. Corpo Fotocamera / Modulo di Scansione Principale -->
  <g filter="url(#card-shadow)">
    <!-- Corpo con sagoma M3 arrotondata -->
    <rect x="120" y="166" width="272" height="204" rx="36" fill="url(#cam-surface)"/>

    <!-- Rialzo del mirino ottico / sensore superiore -->
    <path d="M 180 166 C 180 144, 194 136, 212 136 L 300 136 C 318 136, 332 144, 332 166 Z" fill="url(#cam-surface)"/>

    <!-- Sensore ottico ausiliario / Flash LED M3 -->
    <circle cx="162" cy="198" r="9" fill="#94a3b8" opacity="0.5"/>
    <circle cx="162" cy="198" r="4.5" fill="#cbd5e1"/>

    <!-- Mirino con contrasto -->
    <rect x="320" y="188" width="28" height="18" rx="6" fill="#334155"/>
    <rect x="324" y="191" width="20" height="12" rx="3" fill="#64748b"/>
  </g>

  <!-- 5. Gruppo Ottico / Diaframma Lente Centrale -->
  <g filter="url(#card-shadow)">
    <!-- Ghiera esterna scura -->
    <circle cx="256" cy="274" r="82" fill="url(#bezel-ring)"/>
    
    <!-- Anello metallico intermedio di contrasto -->
    <circle cx="256" cy="274" r="73" fill="none" stroke="#64748b" stroke-width="2.5" opacity="0.6"/>

    <!-- Lame del diaframma con i 4 colori Material 3 -->
    <g opacity="0.95">
      <!-- Lama Superiore (Blu) -->
      <path d="M 256 203 A 71 71 0 0 1 327 274 L 284 274 A 28 28 0 0 0 256 246 Z" fill="url(#iris-blue)"/>
      <!-- Lama Destra (Verde Teal) -->
      <path d="M 327 274 A 71 71 0 0 1 256 345 L 256 302 A 28 28 0 0 0 284 274 Z" fill="url(#iris-teal)"/>
      <!-- Lama Inferiore (Ambra Gialla) -->
      <path d="M 256 345 A 71 71 0 0 1 185 274 L 228 274 A 28 28 0 0 0 256 302 Z" fill="url(#iris-amber)"/>
      <!-- Lama Sinistra (Rosa Corallo) -->
      <path d="M 185 274 A 71 71 0 0 1 256 203 L 256 246 A 28 28 0 0 0 228 274 Z" fill="url(#iris-coral)"/>
    </g>

    <!-- Centro lente in vetro profondo con riflesso -->
    <circle cx="256" cy="274" r="30" fill="url(#lens-glass)"/>

    <!-- Riflesso speculare curvo sulla lente -->
    <ellipse cx="245" cy="263" rx="12" ry="6" transform="rotate(-30 245 263)" fill="#ffffff" opacity="0.7"/>
    <circle cx="267" cy="285" r="3.5" fill="#ffffff" opacity="0.4"/>
  </g>

  <!-- 6. Raggio orizzontale luminoso "Scan AI" -->
  <g filter="url(#scan-glow)">
    <line x1="92" y1="274" x2="420" y2="274" stroke="url(#beam-grad)" stroke-width="4.5" stroke-linecap="round"/>
    <circle cx="256" cy="274" r="6" fill="#ffffff"/>
  </g>

  <!-- 7. Stella AI Material 3 Auto-Awesome (Sparkle) in alto a destra -->
  <g filter="url(#star-glow)">
    <!-- Stella a 4 punte curva (Material Symbol sparkle principale) -->
    <path d="M 386 106 Q 386 138, 418 138 Q 386 138, 386 170 Q 386 138, 354 138 Q 386 138, 386 106 Z" fill="url(#star-gold)"/>
    
    <!-- Stella secondaria più piccola -->
    <path d="M 424 178 Q 424 192, 438 192 Q 424 192, 424 206 Q 424 192, 410 192 Q 424 192, 424 178 Z" fill="url(#star-gold)" opacity="0.85"/>
  </g>
</svg>'''

async def generate():
    root = Path(__file__).resolve().parent.parent
    frontend = root / "frontend"
    fotoscan = root / "fotoscan"
    cache = root / "cache"
    cache.mkdir(parents=True, exist_ok=True)

    # 1. Scrivi il nuovo SVG
    svg_path = frontend / "favicon.svg"
    svg_path.write_text(SVG_CONTENT, encoding="utf-8")
    print(f"Scritto SVG in: {svg_path}")

    # 2. Renderizza a 1024x1024 (512x512 @ 2x) tramite Edge headless
    html_page = cache / "icon_render.html"
    html_page.write_text(f'''<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<style>
  html, body {{
    margin: 0; padding: 0;
    width: 512px; height: 512px;
    background: transparent;
    overflow: hidden;
  }}
</style>
</head>
<body>
{SVG_CONTENT}
</body>
</html>''', encoding="utf-8")

    png_master = cache / "icon_master_1024.png"

    async with async_playwright() as p:
        browser = await p.chromium.launch(channel="msedge")
        page = await browser.new_page(viewport={"width": 512, "height": 512}, device_scale_factor=2)
        await page.goto(f"file:///{html_page.as_posix()}")
        await page.screenshot(path=str(png_master), omit_background=True)
        await browser.close()
    print(f"Screenshot master ad alta risoluzione: {png_master}")

    # 3. Genera le varie versioni PNG con filtro Lanczos ad alta definizione
    img = Image.open(png_master).convert("RGBA")

    # Icone per frontend / PWA
    img.resize((32, 32), Image.Resampling.LANCZOS).save(frontend / "favicon-32.png", format="PNG")
    img.resize((192, 192), Image.Resampling.LANCZOS).save(frontend / "icon-192.png", format="PNG")
    img.resize((512, 512), Image.Resampling.LANCZOS).save(frontend / "icon-512.png", format="PNG")
    print("Salvati PNG in frontend/ (32, 192, 512)")

    # 4. Genera ICO multi-risoluzione per Windows (256, 128, 64, 48, 32, 16)
    ico_sizes = [(256, 256), (128, 128), (64, 64), (48, 48), (32, 32), (16, 16)]
    ico_dest = fotoscan / "icon.ico"
    img.save(ico_dest, format="ICO", sizes=ico_sizes)
    print(f"Salvato icon.ico per Windows in: {ico_dest}")

    # Copia anche in frontend/favicon.ico
    frontend_ico = frontend / "favicon.ico"
    img.save(frontend_ico, format="ICO", sizes=[(48, 48), (32, 32), (16, 16)])
    print(f"Salvato favicon.ico in: {frontend_ico}")

    # 5. Notifica Windows Explorer per aggiornare la cache delle icone
    try:
        SHCNE_ASSOCCHANGED = 0x08000000
        SHCNF_IDLIST = 0x0000
        ctypes.windll.shell32.SHChangeNotify(SHCNE_ASSOCCHANGED, SHCNF_IDLIST, None, None)
        print("Notificato Windows Explorer del cambio icona (SHChangeNotify)")
    except Exception as e:
        print(f"SHChangeNotify non riuscito: {e}")

if __name__ == "__main__":
    asyncio.run(generate())
