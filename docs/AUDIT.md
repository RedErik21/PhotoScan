# PhotoScan — Audit dei problemi (2026-11-28)

Audit in sola lettura di tutto il codice (7 aree, 1 revisore per area + verifica incrociata delle voci più gravi).
Nessun test eseguito, nessuna modifica al codice. ✔ = verificato a mano sul codice; le altre voci sono
riportate dai revisori con riga e scenario, da confermare durante la correzione.

Priorità:
- **P0** — perdita o danno di file dell'utente, oppure funzione principale rotta. Da correggere prima della 0.1.11.
- **P1** — app che si blocca, dati incoerenti nel catalogo, funzione rotta su una piattaforma.
- **P2** — robustezza, casi limite, prestazioni.

Nota di rilascio: la versione 0.1.10 è già pubblicata (tag `v0.1.10`, `version.py` = 0.1.10).
Il prossimo build deve essere **0.1.11**, dal commit dd476b4 o successivo.

---

## P0 — Prima della 0.1.11

### Perdita di file

| # | Dove | Problema | Correzione |
|---|------|----------|------------|
| 1 ✔ | `server/routes_cleanup.py:187-206` | Cestinando il master di un gruppo duplicati, il nuovo master è l'id più basso (`survivors[0]`) anche se è un quasi-duplicato; le copie esatte restano `is_dup=1` verso di lui e «Cestina copie esatte» le butta: il contenuto originale sparisce. | Scegliere il nuovo master tra le copie esatte (`is_dup=1`); riclassificare il resto o ricalcolare il gruppo. |
| 2 ✔ | `server/routes_duplicates.py:297-323` | «Cestina copie» non controlla che il master esista ancora su disco / non sia in `broken`: se il master è sparito, si cestinano le ultime copie. | Saltare il gruppo se il master manca o è rotto. |
| 3 | `server/routes_duplicates.py:66-73, 213-233, 280` | Uno scambio master invalidato (gruppo cambiato) lascia i `file_override`: dopo il rebuild il file scelto dall'utente è `is_dup=2` e «Cestina copie simili» butta proprio quello. | Quando `dup_keep` non è più valido, cancellare anche i suoi override. |
| 4 ✔ | `phone_mtp.py:190` (+ `phone_mtp.ps1:590-610`, `phone_linux.py:175-181`) | Importazione telefono: file parziali (timeout, cavo staccato, Annulla) vengono spostati in destinazione da `_move_into_dest` come file validi. Se l'utente poi svuota il telefono, il file è perso. | Cancellare il parziale se `ok=false`; non chiamare `_move_into_dest` dopo annullamento/errore. |
| 5 | `nas.py:423-488`, `server/routes_status.py:446-451` | Se la cartella NAS coincide con la sorgente (disco USB, junction), copia = originale: «Pulisci» lo considera verificato e lo cestina. | `os.path.samefile(local, target)` in `plan_send` e `cleanup_job`; vietare la sovrapposizione anche cambiando `source_dir`. |
| 6 | `nas.py:446-456` | `SHFileOperationW` cancella **definitivamente** se il Cestino è disattivato o il file è troppo grande, e lo conta come successo. La UI promette il recupero. | `IFileOperation` + `FOFX_RECYCLEONDELETE` (fallisce invece di cancellare). Vale anche per `routes_cleanup._send_to_recycle_bin`. |
| 7 | `phone_mtp.py:124-139, 202-206` | Foto diverse con stesso nome+dimensione considerate «già presenti» e scartate senza confronto. | Confronto hash/data prima di saltare o cancellare dallo staging. |
| 8 | `server/routes_files.py:437-451` | «Rimuovi file danneggiati» cancella dal catalogo (album, preferiti, volti a cascata) senza ricontrollare che il file manchi davvero (vedi #10). | Riverificare l'esistenza o la leggibilità prima di cancellare. |

### Funzioni rotte

| # | Dove | Problema | Correzione |
|---|------|----------|------------|
| 9 ✔ | `photoscan.spec:74-77` | `phone_mtp.ps1` non è nei `datas`: nell'exe Windows l'importazione dal telefono non parte. | Aggiungere `("fotoscan/phone_mtp.ps1", "fotoscan")`. |
| 10 ✔ | `server/routes_media.py:261-304` | Una miniatura fallita marca il file «illeggibile» e lo nasconde dalla Galleria per sempre (mai tolto da `broken`). Senza ffmpeg **tutti i video spariscono**; disco/NAS scollegato → tutto «mancante». | Marcare solo errori di decodifica veri (non ffmpeg mancante o OSError di rete); togliere da `broken` quando la miniatura riesce. |
| 11 ✔ | `updater.py:524` | `start_exe_update` ignora `update_auto_install`: «Installa automaticamente» non fa nulla su exe/AppImage. | Passare `force=cfg.update_auto_install`. |
| 12 ✔ | `launcher/setup_photoscan.py:142-144` | Il Setup fa `rmtree(app_dir)` prima di spostare la versione nuova: con PhotoScan aperto si ferma a metà e l'app non parte più. | Rinominare in `app.old` → spostare la nuova → ripristinare se fallisce; controllare che PhotoScan sia chiuso. |
| 13 ✔ | `faces.py:620-631, 554-572` | Ogni errore (CUDA/cuDNN assenti, OOM, file bloccato) registra la foto in `faces_done` con 0 volti: con una GPU rotta l'intera libreria risulta «analizzata, nessun volto». | Non segnare come fatta se c'è stata un'eccezione; se l'errore è del motore, interrompere il job con un messaggio. |
| 14 ✔ | `server/routes_people.py:602-611` | «Azzera scansione volti» cancella i volti di tutte le persone (rinominare non imposta `locked`): i nomi restano senza volti, il rescan crea persone anonime. Nomi persi. | Prima di cancellare, `locked=1` sui volti delle persone con nome, oppure riaggancio per centroide. |
| 15 | `server/routes_people.py:464, 604` | La cache dei ritagli `cache/faces/{face_id}_{size}.jpg` non viene mai invalidata; gli id vengono riusati dopo un reset, quindi gli avatar mostrano facce di altre persone. | Svuotare `cache/faces` a reset/cambio modello; `AUTOINCREMENT` o versione nell'URL. |
| 16 ✔ | `frontend/js/router.js:83` | `mount()` non riceve `path`: `organize.js:38-40` non riconosce `#/index` e `#/duplicates`. Tutti i link «Vedi dettagli», «Vedi nei duplicati», il chip di stato e la fine dell'onboarding aprono la dashboard invece della pagina giusta. | Passare `{ path, params, query, navigate, signal }`. |
| 17 ✔ | `scanner_metadata.py:212, 288-291` | La data `creation_time` dei video (`2021-05-01T10:00:00`) non viene mai riconosciuta (la regex vuole uno spazio, non `T`): i video prendono la data di modifica del file e finiscono nell'anno o mese sbagliato. È anche in UTC. | Accettare `[T ]`, convertire da UTC all'ora locale. Rianalizzare i video. |
| 18 | `server/routes_search.py:739`, `embedder.py:202` | Dopo un cambio modello gli embedding vecchi restano e `meta.embedding_model` viene sovrascritto: con dimensioni diverse `np.stack` / `mat @ qv` danno 500 su ricerca, piano e suggerimenti. | Rifiutare (409) `index/start` se il modello è diverso e ci sono embedding; 409 in `/search` come `labels_test`. |

### Sicurezza

| # | Dove | Problema | Correzione |
|---|------|----------|------------|
| 19 ✔ | `server/app.py` | Nessun controllo su `Host` e `Origin`: DNS rebinding (una pagina web legge foto e GPS e usa `/api/cleanup/trash`) e CSRF sugli endpoint senza corpo (eliminare album, azzerare volti, approvare un aggiornamento…). | Middleware: `Host` ∈ {127.0.0.1:porta, localhost:porta}; `Origin` uguale per le richieste che modificano dati. |
| 20 ✔ | `frontend/js/ui/snackbar.js:47`, `ui/dialog.js:119,145,190` | Il `title` di SweetAlert2 è HTML: un album, una persona o un'etichetta chiamata `<img src=x onerror=…>` esegue script quando viene eliminata o rinominata. | Usare `titleText` al posto di `title`. |
| 21 | `updater.py` | Zip e exe dell'aggiornamento senza hash né firma. | `SHA256SUMS` firmato nella Release, verificato prima dell'estrazione. |

---

## P1 — Blocchi, incoerenze, piattaforme

### Avvio, aggiornamenti, packaging
- `desktop.py:726-742`: due avvii ravvicinati lanciano due server sullo stesso catalogo; lo stato viene scritto solo quando il server è pronto. Serve un mutex o lock file e uno splash «Avvio in corso».
- `desktop.py:777-778, 191-200`: `taskkill /T /F` sul pid preso da `desktop_server.json` senza verificare che sia PhotoScan: con un pid riciclato può uccidere un processo estraneo. Salvare anche l'ora di creazione del processo, o usare un endpoint di spegnimento.
- `desktop.py:773-784`: chiudere la prima finestra spegne il server della seconda; la seconda istanza lancia l'helper di installazione, che attende 120 s e fallisce.
- `desktop.py:571-612`: senza WebView2, o con `private_mode` di pywebview ≥5 (localStorage non persistente) e porta casuale (`free_port`), le preferenze si azzerano a ogni avvio. Usare `private_mode=False` e `storage_path`.
- `updater.py:193-211, 297-317`: le parti `.001/.002` non sono verificate (contiguità e dimensione totale) su Windows; con una Release pubblicata prima che finisca l'upload si scaricano 4 GB inutili. Nel workflow usare `--draft` e pubblicare alla fine.
- `updater.py:276-317, 135-140`: i file `.part` e l'AppImage scartata non vengono cancellati (fino a 4 GB).
- `launcher/setup_photoscan.py:174-182`: la scorciatoia sul Desktop fallisce con apostrofi nel percorso o con il Desktop su OneDrive, ma l'interfaccia dice «creato».
- `launcher/setup_photoscan.py:108-128`: download da 3–5 GB senza ripresa né nuovi tentativi; un 403 per rate limit compare come errore grezzo.
- `.github/workflows/release.yml`: nessuno smoke test dell'exe costruito.
- **Peso degli aggiornamenti** (richiesta dalla sessione photoscan-06): PyTorch/torchvision/transformers in un asset «runtime» separato, scaricato solo quando cambia; obiettivo: aggiornamenti da circa 0,5 GB. L'export ONNX di SigLIP (B) resta per dopo.

### Linux (AppImage)
- `server/routes_index.py:418`: `taskkill` → FileNotFoundError: «Ferma indicizzazione» dà 500. ✔
- `server/routes_cleanup.py:62`, `nas.py:433`: Cestino solo Windows (pulizia, duplicati, NAS non funzionano).
- `packaging/linux/Dockerfile:7`: build su bookworm (glibc 2.36): l'AppImage non parte su Ubuntu 22.04 / Mint 21.
- `desktop.py:59,82,755`, `routes_status` xdg-open: `LD_LIBRARY_PATH` di PyInstaller ereditato da Chromium, zenity e xdg-open, che vanno in crash.
- `desktop.py:62-69, 765-775`: senza Chromium né zenity il server si spegne appena aperto Firefox.
- `updater.py:509-510`: `os.replace` verso `$APPIMAGE` fallisce su un altro filesystem (EXDEV) senza dirlo e viene ritentato a ogni chiusura; «Installa e riavvia» non riavvia.
- `server/routes_index.py:385`: `subdir.replace("/", "\\")` rompe le sottocartelle.
- `server/routes_apply.py:197`: destinazione suggerita `/PhotoScan_Organizzato` (non scrivibile).
- `nas.py:47-54`: NAS smontato = cartella locale vuota, quindi il «backup» finisce sul disco locale.

### Indicizzazione
- `scanner_metadata.py:438-450`: transazione di scrittura aperta per tutto un lotto da 400 file; durante l'indicizzazione le altre scritture danno «database is locked» dopo 60 s.
- `scanner_metadata.py:423-424`, `embedder.py:229-230`: con un disco staccato a metà, migliaia di file passano a `error` in modo permanente. Controllare `reachable()` e interrompere.
- `embedder.py:39, 250-255`: nessun ripiego su CPU se CUDA fallisce; un OOM marca in errore il blocco e tutti quelli successivi. Dimezzare il batch e ritentare.
- `scanner_metadata.py:94`: cartelle di servizio dei NAS (`@eaDir`, `#recycle`, `$RECYCLE.BIN`, `.Trash*`) indicizzate come foto.
- `scanner_metadata.py:141`, `embedder.py:68`: `_fs()` (percorsi >260 caratteri) manca in stat e Image.open: file scartati senza avviso.
- `server/routes_index.py:71-77`: un pid file obsoleto con pid riciclato blocca l'indicizzazione («già in corso»).
- `cli.py:53-66`: errori di modello/Embedder non registrati in `last_index_error`.

### Piano / applicazione
- `server/routes_cleanup.py:173,188`: query orfani con il doppio delle variabili e senza tetto: oltre circa 16k id → errore dopo che i file sono già nel Cestino, catalogo non aggiornato. Procedere a blocchi.
- Endpoint duplicati e `apply_run` non controllano un rebuild del piano in corso (e viceversa): override sovrascritti, IntegrityError, apply del piano vecchio.
- TOCTOU `running_job` → `start_job` (piano, NAS, volti, suggerimenti, broken check): un doppio clic avvia due job. Serve un `start_job_unique` sotto `_jlock`.
- `organizer.py:573-582, 371-410`: coordinate GPS NaN o fuori intervallo fanno fallire tutto `build_plan`.
- `server/routes_apply.py:104-117`: l'anteprima dichiara «collegamenti, 0 byte» su FAT32/exFAT/SMB, dove i link falliscono e si copia; lo spazio non viene ricontrollato in `apply_run` e il job non si ferma con il disco pieno.
- `organizer.py:745-749`: un file modificato (hash NULL) genera una nuova copia `nome_<id>` a ogni applicazione.
- `server/routes_apply.py:274-275`: il fingerprint del piano è facoltativo e non copre nomi e `is_dup`.

### Volti
- `faces.py:100-111, 321`: nessun ripiego su CPU se CUDA è dichiarata ma non utilizzabile.
- `faces.py:187, 219-223`: il fd di `mkstemp` non viene chiuso, quindi su Windows lo ZIP da 280 MB resta in cache.
- `faces.py:512-520`: cambiare `FACES_MODEL_ID` cancella le persone con nome, il merge_log e le coppie «Diverse».
- `faces.py:778-864`: `cluster_faces` tiene il lock di scrittura per minuti, mentre le altre scritture danno «database is locked».
- Indicizzazione e job volti possono girare insieme (`_reset_changed` contro volti appena inseriti).
- `faces.py:1033-1046`: «Annulla esclusione» non riporta il volto alla sua persona.
- Le unioni perdono le coppie `persons_not_same` (CASCADE) e non le ripristinano con Annulla.
- Cancellando file con volti non vengono ricalcolati `persons.n`, il centroide e `cover_face_id`; `faces_done` resta orfano.

### Server / API
- `server/routes_status.py` `/status`, `/system/disk`, `/nas/status`, `routes_index._snapshot`: I/O di rete senza timeout negli endpoint sync; un NAS spento satura il threadpool e blocca tutta l'app.
- `scanner_metadata.py:39`: `MAX_IMAGE_PIXELS = None` con 8 thread, quindi rischio OOM con immagini enormi o malevole.
- `server/routes_media.py:35`: un solo pool da 8 thread per miniature, ffmpeg e streaming; il play di un video aspetta in coda.
- `server/routes_media.py:355-405`: handle dei file aperto durante lo streaming; su Windows il file non si può spostare né cestinare.
- `config.py:296-312`: un `config.json` corrotto o con BOM viene ignorato in silenzio e poi sovrascritto con i default; `cache_dir` irraggiungibile fa fallire ogni richiesta.
- `server/routes_status.py:~257`: `model_name` e `device` accettati senza validazione.

### Telefono / NAS / rete
- `phone_mtp.ps1:592-609`, `phone_mtp.py:168`: telefono scollegato = 300 s per file senza interruzione, «Annulla» non risponde; con `expected<0` vengono accettati file troncati.
- `phone_mtp.py:34`: la lista `skip` passa in una variabile d'ambiente (limite 32.767 caratteri); oltre circa 600 foto «Solo le nuove» fallisce.
- `nas.py:39-54, 172-354`: nessun timeout SMB; un job bloccato in `copyfile` blocca invio e pulizia fino al riavvio.
- `phone_mtp.py:113-139`: rinomina `IMG_2.jpg` → a ogni import un duplicato in più.
- `nas.py:486-488`, `server/routes_nas.py:265`: la pulizia verifica solo il **numero** di file dell'anteprima, non l'elenco.
- `sourcefs.py:76-112`: `rebase_catalog` riscrive tutti i percorsi con il 50% di corrispondenze.
- `netguard.py:48-49`: nell'exe il registro rete finisce in `_MEIPASS/cache`, cioè `_internal`.
- `netguard.py:318-326`, `server/routes_status.py:64-72`: una cache del modello corrotta non fa ripartire il download; uno snapshot con il solo `config.json` risulta «in cache».

### Frontend
- `cleanup.js:204`: `selection.toggle(fid, false)` inverte la selezione invece di togliere l'elemento.
- `cleanup.js:~173-222`, `gallery.js:776`, `albums.js:297`: nessuna protezione dal doppio clic su Cestino ed Elimina.
- `albums.js:529`, `viewer.js:605`: i file non spostati scompaiono dalla UI come se fossero stati cestinati.
- `main.js:367-407`: 4 tentativi in 2,3 s su `/status`, poi l'app resta ferma anche quando il server torna.
- `gallery.js:142-408`, `update-notice.js`: localStorage senza try/catch, quindi la Galleria non si apre se lo storage è bloccato.
- `settings.js:407-423`: le scorciatoie da tastiera scartano le modifiche non salvate senza conferma.

---

## P2 — Robustezza e casi limite (selezione)
- Nomi riservati Windows (`CON`, `NUL`…) e lunghezza dei percorsi in `safe_name`/`safe_folder`; cartelle unite dopo la sanificazione; `_is_protected` sensibile alle maiuscole.
- Nome `nome_<id>` in collisione non ricontrollato per contenuto (`organizer.py:742-750`).
- Residui `*.fotoscan-tmp` dopo un kill; `_copy_atomic` usa `os.replace`, che può sovrascrivere.
- `plan_reset_overrides` senza `confirm`; GET dei duplicati con scritture (`_write_back`).
- Annullare a lavoro finito segna il job come «cancelled».
- `datetime.fromtimestamp` con mtime negativo o enorme fa fallire tutto `process_metadata`.
- Con ffprobe assente durata e dimensioni non vengono mai ricalcolate; FAT32 e ora legale portano a una reindicizzazione completa.
- id oltre 2^63, cifre unicode, NUL nei percorsi → 500 al posto di 4xx; timeline con date malformate → 500.
- Miniature a 16 bit e con canale alpha; cache dei fotogrammi senza versione.
- GZip applicato anche a JPEG e video.
- Race nella migrazione DB al primo avvio dopo un aggiornamento (`ALTER TABLE` concorrenti).
- Album senza UNIQUE sul nome; `index.log` mai ruotato; `rete.jsonl` con rotazione non atomica tra processi.
- Errori dei job (`job.error`) con percorsi inviati al client; job `daemon=True`.
- Dipendenze non bloccate in `requirements.txt`.
- `parse_version` ignora la quarta cifra e le etichette prerelease.
- Le scorciatoie globali restano attive con il visualizzatore o una modale aperti.

---

## Ordine di lavoro consigliato per la 0.1.11
1. Sicurezza: #19 (Host/Origin), #20 (`titleText`) — poche righe ciascuna.
2. Perdita dati: #1, #2, #3, #4, #5, #6, #8.
3. Funzioni rotte, una riga o poco più: #9 (spec), #11 (auto-install), #16 (router), #17 (data video).
4. #10 (file nascosti per sempre), #12 (Setup), #13–#15 (volti), #18 (cambio modello).
5. Smoke test dell'exe nel workflow di release, poi il build 0.1.11.
