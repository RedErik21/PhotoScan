# PhotoScan — guida per sviluppatori

Questa pagina raccoglie tutto ciò che serve a chi modifica il codice o pubblica le versioni.
Per l'uso normale vedi il [README](../README.md).

## Installazione dal codice

Requisiti: Python 3.13 e, consigliata, una scheda NVIDIA.

```powershell
git clone https://github.com/RedErik21/PhotoScan.git
cd PhotoScan
copy config.example.json config.json   # facoltativo: l'app lo crea al primo avvio
python -m venv .venv
.\.venv\Scripts\activate
pip install torch torchvision --index-url https://download.pytorch.org/whl/cu126   # GPU NVIDIA
# oppure, solo CPU:  pip install torch torchvision
pip install -r requirements.txt
winget install Gyan.FFmpeg          # per i video (fotogrammi e durata)
```

Il modello (`google/siglip2-base-patch16-224`, circa 400 MB) viene scaricato da Hugging Face al primo
utilizzo. Per le foto HEIC serve `pillow-heif` (già in `requirements.txt`). La finestra usa Microsoft Edge
(presente su Windows 10/11) oppure Google Chrome.

Una copia ottenuta con `git clone` può cercare gli aggiornamenti ma non installarli: si aggiorna con `git pull`.

## Avvio dal terminale

```powershell
.\.venv\Scripts\pythonw.exe -m fotoscan.desktop    # stessa cosa dell'icona
.\.venv\Scripts\python.exe -m fotoscan ui          # stessa cosa, dal terminale
```

Per la prima prova si può indicizzare solo un campione di file:

```powershell
.\.venv\Scripts\python.exe -m fotoscan index --source "D:\Foto" --limit 300
```

L'indicizzazione completa si avvia dall'app (sezione **Indicizzazione**) oppure con
`python -m fotoscan index`. È riprendibile: i file già elaborati non vengono rifatti.

## Comandi da terminale

```powershell
python -m fotoscan index [--source ...] [--limit N] [--subdir ...]   # scansione + metadati + embedding
python -m fotoscan plan                  # calcola o ricalcola il piano
python -m fotoscan apply                 # simulazione (dry-run)
python -m fotoscan apply --apply --output "E:\Organizzato"           # crea davvero le cartelle
python -m fotoscan stats                 # conteggi del catalogo
python -m fotoscan ui                    # applicazione (server + finestra)
python -m fotoscan serve [--port 8765]   # solo il server, per sviluppo: stampa l'URL
python -m fotoscan ui-legacy             # vecchia interfaccia Streamlit (solo di riferimento)
```

## Struttura del progetto

| Percorso | Contenuto |
| --- | --- |
| `fotoscan/config.py` | percorsi, estensioni, soglie, etichette di contesto, dispositivo (`config.json`) |
| `fotoscan/scanner_metadata.py` | scansione, EXIF/GPS, data da nome file o da sidecar Takeout, hash, fotogrammi video (ffmpeg) |
| `fotoscan/embedder.py` | SigLIP: embedding di immagini, fotogrammi e testo; pipeline riprendibile |
| `fotoscan/faces.py` | riconoscimento volti locale (`onnxruntime-gpu` + `buffalo_l` SCRFD/ArcFace, clustering stabile `persons`) |
| `fotoscan/organizer.py` | piano `Anno/MM - Contesto`, eventi, duplicati, applicazione con link o copia |
| `fotoscan/db.py` | catalogo SQLite (`catalogo.db`) |
| `fotoscan/server/` | server FastAPI: `app.py` (core), `deps.py`, `jobs.py` e un file `routes_*.py` per gruppo di API |
| `fotoscan/desktop.py` | avvio come applicazione: server nascosto + finestra Edge/Chrome in modalità app |
| `fotoscan/updater.py` | aggiornamenti: controllo delle Release GitHub, download, installazione con copia di riserva |
| `fotoscan/version.py` | versione installata e repository degli aggiornamenti |
| `fotoscan/cli.py` | comandi `python -m fotoscan ...` |
| `fotoscan/app_streamlit.py` | vecchia interfaccia Streamlit (solo `ui-legacy`) |
| `frontend/` | interfaccia in ES modules e Material Design 3, senza build (`index.html`, `js/`, `css/`, `vendor/`) |
| `launcher/` | programma di installazione `PhotoScan-Setup` (pywebview con ripiego Tkinter) |
| `site/` | pagina di presentazione IT/EN |
| `tools/devserver.py` | server di sviluppo isolato per un singolo agente o sessione |
| `tools/shot.py` | screenshot di una pagina con Edge (Playwright) |
| `tools/run_tests.py` | esegue tutti i test `tests/test_*.py` e stampa il riepilogo |
| `tools/demo/` | script per rigenerare la libreria demo, gli screenshot e il video |
| `tests/` | test automatici (API, date, piano, apply) su dati finti e cartelle temporanee |
| `docs/CONTRACT.md` | contratto tra backend e frontend: API, oggetto `File`, convenzioni |
| `docs/STATO.md` | stato dell'implementazione: funzioni, limiti noti, come avviare e testare |

I dati locali (`catalogo.db`, `cache/`, `config.json`, `fotoscan_ui.log`) restano nella cartella del
progetto e non vanno condivisi.

## Sviluppo

Server e interfaccia:

```powershell
.\.venv\Scripts\python.exe -m fotoscan serve --port 8765     # stampa l'URL, Ctrl+C per fermare
# documentazione interattiva delle API: http://127.0.0.1:8765/api/docs
```

Il frontend viene letto da disco a ogni richiesta: basta ricaricare la pagina. Dopo modifiche al
backend, riavviare il server.

Server isolato di sviluppo (copia del catalogo, il catalogo vero non viene mai scritto):

```powershell
.\.venv\Scripts\python.exe tools\devserver.py start --name prova --port 9101
.\.venv\Scripts\python.exe tools\devserver.py restart --name prova --port 9101
.\.venv\Scripts\python.exe tools\devserver.py stop --name prova
```

Screenshot (richiede `playwright`: decommentarlo in `requirements.txt` e reinstallare):

```powershell
.\.venv\Scripts\python.exe tools\shot.py --url "http://127.0.0.1:9101/#/gallery" --out "C:\temp\gallery.png" --width 1280 --height 800
```

Opzioni utili: `--dark` (tema scuro), `--width 390 --height 844` (telefono), `--full` (pagina intera).

Test:

```powershell
.\.venv\Scripts\python.exe tools\run_tests.py              # tutti i test
.\.venv\Scripts\python.exe tools\run_tests.py api_files    # solo i file che contengono il testo
```

Il venv non include pytest: i test sono script autonomi. Il comando termina con codice 0 solo se
tutti i file passano.

## Versione Linux (AppImage)

Il pacchetto Linux è un unico file `PhotoScan-<versione>-x86_64.AppImage`, costruito dentro Docker: chi lo
costruisce ha bisogno di Docker, chi lo usa no.

```powershell
docker build -t photoscan-linux-build -f packaging/linux/Dockerfile .
docker run --rm --entrypoint cat photoscan-linux-build /out/PhotoScan-<versione>-x86_64.AppImage > PhotoScan-<versione>-x86_64.AppImage
```

Note di progetto:
- la finestra è il browser predefinito in modalità app (Chromium/Chrome/Edge/Brave se presenti); niente GTK o Qt;
- l'import da telefono legge la cartella montata da GNOME o KDE (gvfs): nessun programma esterno;
- l'aggiornamento scarica l'AppImage nuovo e lo mette al posto di quello in uso (`$APPIMAGE`), con copia di riserva;
- ffmpeg statico è incluso nel pacchetto (video);
- PyTorch è solo CPU: il pacchetto Linux non include CUDA.

## Pubblicare una nuova versione

1. Una volta sola: in `fotoscan/version.py` impostare `UPDATE_REPO = "proprietario/repository"`.
2. Alzare `__version__` in `fotoscan/version.py` e fare commit.
3. Creare e spingere il tag: `git tag v0.1.4` e `git push origin v0.1.4` (con `-beta.1` diventa una versione di prova).

Il workflow `.github/workflows/release.yml` verifica che il tag corrisponda a `__version__` e crea la Release;
le note sono generate dai commit e compaiono nell'app.

## Problemi tecnici

- **«Il server di PhotoScan non è partito»:** il primo avvio può richiedere fino a 2 minuti (caricamento di
  torch). Se il log riporta un errore di importazione, completare l'installazione con
  `pip install -r requirements.txt` dentro il venv.
- **Video saltati:** ffmpeg non trovato. Installare `Gyan.FFmpeg` con winget e riavviare il terminale.
- **Nessun Edge né Chrome:** PhotoScan si apre nel browser predefinito e compare una finestra con il
  pulsante OK: premerlo per spegnere il server.
- **Errori di singoli file:** elencati nella sezione **Indicizzazione** (o `GET /api/index/errors`);
  si possono riprovare con `POST /api/index/retry-errors`.
- **Vecchia interfaccia Streamlit:** `python -m fotoscan ui-legacy`, solo per confronto.
