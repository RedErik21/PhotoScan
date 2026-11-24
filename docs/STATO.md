# FotoScan 2 — Stato dell'implementazione

Stato: **implementato** (rifiniture finali completate, test verdi). Contratto API e convenzioni: `docs/CONTRACT.md`.

## Funzioni

- **Galleria** (`/gallery`): griglia virtualizzata per data, scrubber per anno/mese, filtri Tutte/Foto/Video/Preferiti,
  densità regolabile. Le date incerte (ricavate dalla data di modifica del file) sono in fondo, nel gruppo "Data incerta".
- **Visualizzatore**: schermo intero, zoom e spostamento, scorrimento, frecce e ESC, pannello info, preferito, aggiunta ad album, controlli video.
- **Ricerca semantica** (`/search`): ricerca a parole con SigLIP2 su CPU, suggerimenti, prova delle etichette di contesto.
- **Cartelle proposte** (`/folders`): anteprima del piano `Anno/MM - Contesto`, eventi, varie, rinomina, unione, spostamento foto.
- **Duplicati** (`/duplicates`): gruppi esatti e quasi-uguali, scelta del file da tenere.
- **Album**, **Persone** (`/people`) e **Luoghi** (mappa GPS a griglia), **Statistiche**, **Ricordi**.
  - **Riconoscimento volti**: basato su `onnxruntime-gpu` + modelli InsightFace `buffalo_l` (`det_10g.onnx` SCRFD + `w600k_r50.onnx` ArcFace, caricamento ONNX diretto senza pacchetto `insightface`, 100% locale su GPU NVIDIA CUDA o CPU; pesi con licenza non commerciale per uso personale/open-source). Clustering stabile con tabella `persons`, rinomina, unione, nascondi, esclusione falsi volti, ritagli faccia in cache e filtro per persona in Galleria (`?person=ID`).
- **Crea cartelle** (`/apply`): anteprima e applicazione con conferma esplicita; collegamenti (hard link) o copia.
- **Indicizzazione** (`/index`): avvio, arresto, riprendibile, errori con riprova.
- **Impostazioni** e **Aiuto**: cartelle, regole, etichette, tema chiaro/scuro/sistema.
- **Shell adattiva**: barra inferiore (compatto, <600 px), rail (600–1023 px), sidebar 264 px (≥1024 px, comprimibile e ricordata).
  Sidebar in tre gruppi: **Libreria** (Home, Foto, Cerca, Album, Luoghi, Statistiche), **Organizza** (4 passi numerati con stato:
  Analisi → Duplicati → Cartelle → Crea cartelle), piede con stato dell'archivio, Impostazioni e Aiuto. Sulle pagine del flusso
  compare una barra dei passi con il pulsante «Avanti». Aree di tocco di 48 px su dispositivi touch.
- **Tema grafico** «Indaco»: `css/tokens-color.css` (palette chiaro/scuro), `css/theme.css` (livello visivo dei componenti, caricato per ultimo),
  font di sistema Segoe UI Variable (ripiego Roboto locale).

## Limiti noti

- Zoom oltre 1:1 con l'originale nel visualizzatore: **non implementato** (la vista usa la versione ridimensionata).
- HEIC/TIFF non si visualizzano nel browser: la vista usa la versione JPEG generata dal server.
- L'avvio come applicazione (`fotoscan.desktop`, scorciatoia sul Desktop) non è stato riverificato end-to-end
  in questa sessione: le verifiche sono state fatte sul server di sviluppo isolato.
- Scrubber su schermi compatti: le etichette degli anni sono nascoste (restano tacche e tooltip durante il trascinamento).
- I test di tempo hanno soglie di regressione grossolane (50–150 ms), con il minimo di più campioni: su macchine molto cariche
  possono essere sensibili.
- Il riconoscimento volti è opzionale (`enable_faces`, disattivato di default): i pesi `buffalo_l` (~280 MB) si scaricano una sola volta in `cache/models/faces/` da Impostazioni o dalla pagina Persone.

## Come avviare

```powershell
.\.venv\Scripts\pythonw.exe -m fotoscan.desktop        # stessa cosa dell'icona FotoScan sul Desktop
.\.venv\Scripts\python.exe -m fotoscan serve --port 8765   # solo server, per sviluppo
```

Server di sviluppo isolato (copia del catalogo, il catalogo vero non viene scritto):

```powershell
.\.venv\Scripts\python.exe tools\devserver.py start --name prova --port 9101
.\.venv\Scripts\python.exe tools\devserver.py stop --name prova
```

## Come testare

```powershell
.\.venv\Scripts\python.exe tools\run_tests.py              # tutti i test (tests\test_*.py)
.\.venv\Scripts\python.exe tools\run_tests.py api_files    # solo i file che contengono il testo
```

Screenshot di verifica (richiede Edge e Playwright):

```powershell
.\.venv\Scripts\python.exe tools\shot.py --url "http://127.0.0.1:9101/#/gallery" --out "C:\temp\g.png" --width 390 --height 844 --dark
```

Controlli statici: `node --check` su ogni `frontend/js/**/*.js`; `python -m compileall -q fotoscan`.
