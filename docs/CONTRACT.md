# PhotoScan 2 — Contratto di sviluppo (API, frontend, proprietà dei file)

**Stato: implementato.** API, oggetto `File`, interfacce UI condivise e shell sono in uso; i test in `tests/` passano. Dettagli, limiti noti e come testare: `docs/STATO.md`.

App locale: **backend FastAPI** (`fotoscan/server/`) + **frontend Material Design 3** (`frontend/`, ES modules, nessuna build)
mostrato in una finestra "applicazione". Il motore (scansione, embedding, piano) esiste già in `fotoscan/*.py`: si **riusa**, non si riscrive.
Tutta l'interfaccia è in **italiano**. L'app **non cancella mai** file dell'utente e **non scrive mai** nella cartella sorgente.

## 0. Regole per ogni agente (OBBLIGATORIE)
1. Modifica **solo i file che possiedi** (§6). Se ti serve altro, descrivilo nel report finale; non toccarlo. Per file condivisi usa solo lo strumento **Edit** (mai riscrivere interi file altrui).
2. La cartella sorgente delle foto originali è **SOLA LETTURA**. Mai `apply` reale, mai indicizzazioni complete, mai toccare `catalogo.db`/`cache/`/`config.json` veri.
3. Lavora con **il tuo server isolato**: `.\.venv\Scripts\python.exe tools\devserver.py start --name <tuonome> --port <tuaporta>` (copia il catalogo vero in una cartella tua). Dopo modifiche al backend: `restart`. Il frontend si ricarica da disco.
4. **Verifica con i tuoi occhi**: `tools\shot.py` fa screenshot con Edge (vedi docstring); poi **leggi l'immagine con Read**. Controlla chiaro e scuro (`--dark`), larghezza telefono (`--width 390 --height 844`) e desktop (1280). Zero errori in console. Il lavoro non è finito finché non l'hai visto funzionare su dati reali (14.245 file).
5. Niente nuove dipendenze (Python: fastapi, uvicorn, pillow, pillow-heif, numpy, torch, transformers già presenti; frontend: solo `@material/web` già in `vendor/`). Niente CDN, niente rete: tutto locale.
6. Codice pulito e commentato in modo essenziale; nessun `console.log` residuo; ogni errore API mostrato all'utente in italiano (snackbar), mai eccezioni non gestite.
7. Rispondi con un report breve: cosa hai fatto, come l'hai verificato (comandi/screenshot), limiti noti, cosa serve da altri.

## 1. Oggetto `File` (JSON)
```json
{"id":1,"name":"IMG_0624.HEIC","kind":"image|video","size":2400000,"path":"D:\\...","date":"2020-06-09T09:28:06"|null,
 "date_source":"exif|sidecar|filename|video|mtime","date_uncertain":false,"lat":41.9|null,"lon":12.4|null,
 "width":4032,"height":3024,"duration":null|12.5,"folder":"2020/06 - Evento 2020-06-09"|null,"context":"Mare e spiaggia"|null,
 "score":0.41|null,"is_dup":0|1|2,"dup_of":null|<id>,"favorite":false,
 "thumb":"/api/files/1/thumb","display":"/api/files/1/display","media":"/api/files/1/media"}
```
`is_dup`: 0 normale, 1 duplicato esatto, 2 quasi-duplicato. Costruito da `deps.file_to_dict(row)` con la query base `deps.SELECT_FILE`.
Lista paginata: `{"total":N,"items":[File,...]}`.

## 2. API (tutte sotto `/api`, JSON; errori `{"detail":"messaggio italiano"}` con status HTTP corretto)
Ogni gruppo vive in **un file `fotoscan/server/routes_<gruppo>.py`** con `router = APIRouter(prefix="/api", tags=[...])` (scoperta automatica).
**Sicurezza (middleware in `app.py`)**: il server risponde solo a richieste con `Host` 127.0.0.1, localhost, [::1] (qualsiasi porta; `testserver` per i test), altrimenti 403 `{"detail":"Host non consentito."}` (difesa da DNS rebinding). Le richieste non GET/HEAD/OPTIONS con `Origin` presente e diverso dalla stessa origine → 403 `{"detail":"Origine non consentita."}`; `Origin` assente è ammesso. Gli id oltre 2^63-1 danno 404/422, mai 500. GZip è applicato a JSON e testo ma non a miniature, anteprime, originali, fotogrammi e ritagli volto.
Già presenti (core, non modificare): `GET /health`, `GET /jobs`, `GET /jobs/{id}`, `POST /jobs/{id}/cancel`, `GET /events` (SSE: `job`, `index`, `hello`).
Per i job: `from . import jobs; job = jobs.start_job("nome", fn)`; `fn(job)` chiama `job.update(stage=, done=, total=, message=)` e controlla `job.cancel_requested`. Ritorna `job.public()` al client.

### routes_status.py
- `GET /status` → `{version, first_run:bool (nessun source_dir o catalogo vuoto), source_dir, output_dir, db_path, counts:{totale,foto,video,indicizzati,errori,duplicati,data_incerta,senza_data_exif}, plan:{built:bool, folders:int, files:int}, device, ffmpeg:bool, model, model_cached:bool, faces:{available,reason,models_ready,provider,model_id}, faces_available:bool, faces_reason}` (usa `db.stats`).
- `GET /settings` → impostazioni editabili `{source_dir,output_dir,model_name,device,batch_size,workers,score_threshold,score_temperature,event_gap_hours,max_event_days,min_event_size,near_dup_threshold,near_dup_seconds,near_dup_name_rule,dup_mode("subfolder|skip"),link_mode("hardlink|copy"),folder_template,labels:[{name,prompt}],null_prompts:[str],video_fractions:[..],enable_faces,faces_threshold,faces_assign_threshold,faces_min_photos,faces_det_size}`.
- `PUT /settings` (rifiuta anche `source_dir` che coincide o si sovrappone a `nas_dir`, `model_name` non presente in `/status.available_models`, `device` cuda/mps non disponibile; corpo parziale, validazione con messaggi italiani, salva `config.json`, chiama `deps.reload_cfg()`), → settings aggiornate.
- `POST /settings/reset-labels` → ripristina le etichette di default.
- `POST /system/pick-folder` `{title?, initial?}` → `{path|null}` (finestra di scelta cartella nativa via tkinter in thread; mai bloccare il loop).
- `POST /system/open-folder` `{path}` → apre in Esplora risorse (solo percorsi esistenti dentro `output_dir` o `source_dir`).
- `GET /system/disk?path=` → `{exists, free_bytes, total_bytes, drive, same_volume_as_source:bool}`.

### routes_files.py
- `GET /files?offset=0&limit=120&kind=image|video&year=&month=&day=YYYY-MM-DD&favorite=1&folder=<piano>&dup=hide|only&sort=date_desc|date_asc|name&uncertain=1|0&person_id=` → lista paginata. Default: `dup=hide` (nasconde `is_dup>0`), `sort=date_desc`. Solo `status='embedded'`.
- `GET /files/{id}` → `File` + `{hash,status,group:[File,...] (stesso hash o dup_of), exif_summary:{camera?,...}|null}`.
- `GET /files/ids?...stessi filtri...&limit=5000` → `{ids:[...]}` (per selezione "tutti").
- `GET /timeline/buckets?granularity=month|day&kind=&favorite=&dup=hide` → `{buckets:[{key:"2020-06"|"2020-06-09", year, month, day?, count, offset}] , total}` ordinati dal più recente, `offset` = posizione cumulativa nella lista `sort=date_desc` (serve a scrubber/griglia virtuale). Senza data: bucket `key:"unknown"` in coda.
- `GET /timeline/years` → `[{year,count}]`.
- `GET /memories?limit=` → "Ricordi": file di oggi (stesso giorno/mese) negli anni passati `[{year, items:[File,...]}]`.
- `POST /files/{id}/favorite` `{value:bool}` → `{favorite}`; `POST /files/favorite` `{ids:[],value}` → `{changed}`.
- `GET /library/broken` → `{total,mancanti,illeggibili}`; `POST /library/broken/check` → job `broken_check` (un solo job alla volta, segna solo i file con cartella raggiungibile); `POST /library/broken/remove` `{confirm:true}` → `{removed,recovered,skipped}`: riverifica ogni voce, toglie dal catalogo solo i file davvero assenti/illeggibili, toglie il segno ai file ritrovati (`recovered`) e salta quelli su dischi non raggiungibili (`skipped`).

### routes_media.py
- `GET /files/{id}/thumb?size=256` → JPEG quadrato ritagliato al centro (size ∈ 96..512), cache su disco `cache/thumbs/`, generazione on-demand in thread pool, foto (HEIC incl. via pillow-heif, EXIF orientation) e video (fotogramma da `cache/frames/{id}_1.jpg` o ffmpeg al volo). Header `Cache-Control: public, max-age=31536000, immutable`, ETag. Fallimento → PNG segnaposto 200 (mai 500 per un file corrotto). Il file è segnato in `broken` come `illeggibile` solo se è davvero danneggiato (non se manca ffmpeg, manca pillow-heif, l'immagine supera 500 Mpx o la lettura fallisce per errore di sistema/rete) e come `mancante` solo se la cartella è raggiungibile (un disco scollegato non conta); il segno si toglie da solo quando la miniatura riesce o il file torna. Immagini a 16 bit sono scalate a 8 bit.
- `GET /files/{id}/display?max=2048` → JPEG ridimensionato (visualizzatore; HEIC/TIFF non mostrabili dal browser), cache `cache/display/`. 404 se l'anteprima è impossibile per cause non legate al file (ffmpeg/formato non supportati, immagine enorme).
- `GET /files/{id}/media` → file originale in streaming con **HTTP Range** (206) per video; `Content-Type` corretto; sola lettura. Rifiuta percorsi non presenti nel catalogo. Range non soddisfacibile → 416; file da 0 byte → 200 vuoto; Range multipli → 200 con tutto il file. Il file si chiude anche se il client si disconnette.
- `GET /files/{id}/frames` → `[url,...]` dei fotogrammi in cache del video (anteprima scorrimento).

### routes_search.py
- `GET /search?q=&limit=60&offset=0&kind=` → `{total, items:[File + {"match":0.31}]}` ricerca semantica (embedding testo SigLIP2 su CPU, caricamento lazy una volta, matrice embedding in cache invalidata dal numero di righe). Query vuota → 400. 409 se gli embedding in catalogo hanno una dimensione diversa dal modello corrente (rifare l'analisi).
- `GET /search/status` → `{model_loaded, loading, indexed}`; `POST /search/warmup` avvia il caricamento in background.
- `POST /labels/test` `{prompt}` → `{items:[File+match] (top 24), coverage:{above_threshold:int}}` per provare un'etichetta prima di salvarla.
- `GET /search/suggestions` → `["tramonto","torta di compleanno",...]` (etichette + frasi comuni).

### routes_plan.py
- `GET /plan/summary` → `{built:bool, total, groups:[{folder, kind:"context|event|varie|incerta|dup|similar", count, videos, bytes, first:"data", last:"data", cover_id}], counts:{context,event,varie,incerta,dup,similar}}`.
- `POST /plan/rebuild` → job (`organizer.build_plan`, modello CPU solo se servono nuovi prompt) → `{job_id}`; al termine `job.result` = dict di `build_plan`.
- `GET /plan/folder?name=&offset=0&limit=120&sort=date` → lista paginata dei file del gruppo + `{folder, count}`.
- `POST /plan/rename` `{old,new}` (unisce se `new` esiste; `organizer.rename_group`) → summary del gruppo.
- `POST /plan/move` `{file_ids:[],folder}` (`organizer.move_files`).
- `POST /plan/reset-overrides` `{confirm:true}` (senza conferma 400; irreversibile) → svuota `rename_map`, `file_override` e i master scelti a mano (`dup_keep`) e ricalcola; 409 se il piano è in ricalcolo o in applicazione.
- `GET /plan/folders/tree` → albero `[{name:"2020", count, children:[{name:"06 - Evento ...", folder:"2020/06 - ...", count}]}]`.

### routes_duplicates.py
- `GET /duplicates?type=exact|similar&offset=0&limit=20` → `{total, groups:[{master:File, members:[File], reason:"hash|nome|immagine", similarity:0.99|null}]}` (da tabella `plan`: `is_dup`, `dup_of`).
- `POST /duplicates/keep` `{master_id, member_ids:[...]}` → riassegna il master (scambia ruoli nel piano via `file_override`); non tocca file su disco.
- `POST /duplicates/keep-many`, `POST /duplicates/keep-largest`: come `keep`. `keep`, `keep-many`, `keep-largest` e `trash-copies` rispondono 409 durante il ricalcolo del piano.
- `POST /duplicates/trash-copies` `{dry_run?, confirm?}` → copie nel Cestino. Risposta con `skipped_groups` (gruppi saltati) anche in `dry_run` (`{groups,files,bytes,skipped_groups,dry_run}`); eseguita: `{trashed,freed_bytes,errors,missing,groups,skipped_groups}`.
- `GET /duplicates/summary` → `{exact:{groups,files,bytes}, similar:{groups,files,bytes}}` (byte recuperabili se si eliminassero).

### routes_index.py
- `GET /index/status` → `{running, stage:"scansione|metadati|embedding|volti|completato|", done, total, percent, eta_seconds|null, rate_per_sec|null, started, log_tail:[str], stats:{...db.stats}}`.
- `POST /index/start` `{limit?:int, subdir?:str}` → avvia `python -m fotoscan index` come **sottoprocesso nascosto** (riprendibile), `{started:true}` (409 se già in corso, se il modello impostato non coincide con quello degli embedding in catalogo, o se c'è un job volti attivo; 400 se `source_dir` mancante/non esiste o `subdir` non valida). `/index/status.error_code` (dopo un fallimento, a indicizzazione ferma) può essere `model_mismatch`, `index_failed`, `source_missing` (oltre a `network_unreachable`).
- `POST /index/stop` → termina l'albero di processi (sicuro: riprendibile).
- Pubblica eventi SSE `index` (stesso corpo di `/index/status`) ogni ~1 s mentre gira (`jobs.bus.publish("index", ...)`), leggendo la meta `progress` del catalogo e `index.log`.
- `GET /index/errors?limit=200` → `[{path,error}]` file in stato `error`; `POST /index/retry-errors` → riporta a `meta` gli errori (riprovabili).

### routes_apply.py
- `GET /apply/preview` → `{ok, total, link, copies, existing, extra_bytes, same_volume, free_bytes, output_dir, source_dir, link_mode, warnings:[str], folders:int, duplicates_to_folder:int, hardlink_supported:bool}` (`organizer.apply_plan(apply=False)`, calcolo in cache breve). Errori di validazione (`output_dir` dentro sorgente...) → `{ok:false, error:"..."}` con 200.
- `POST /apply/run` `{confirm:true}` → job con progresso (applica davvero, **solo con `confirm:true` esplicito**), `{job_id}`; 409 se già in corso, se il piano è in ricalcolo o se lo spazio libero non basta; il risultato del job può contenere `spazio_esaurito` (disco pieno durante la scrittura). `fingerprint` dell'anteprima dipende anche da nome, dimensione, hash e ruolo duplicato dei file. Se la destinazione non supporta gli hard link (FAT32/exFAT/rete) `hardlink_supported:false`, si copia e c'è un avviso.
- `GET /apply/log?tail=200` → ultime righe di `<output>/_fotoscan_apply.log`.

### routes_library.py (preferiti già in routes_files)
- `GET /albums` → `[{id,name,count,cover_id,created}]`; `POST /albums` `{name}`; `PUT /albums/{id}` `{name?,cover_file_id?}`; `DELETE /albums/{id}` (solo l'album, mai i file).
- `GET /albums/{id}/files?offset=&limit=` → lista paginata; `POST /albums/{id}/items` `{file_ids:[]}`; `DELETE /albums/{id}/items` `{file_ids:[]}`.
- `GET /files/{id}/albums` → album che contengono il file.

### routes_hardware.py
- `GET /hardware[?refresh=1]` → `{hardware:{os,cpu:{name,cores,threads},ram_gb,gpus:[..],gpu:{name,vram_gb,driver,compute_cap}|null,torch:{installed,version,cuda_build},device:"cuda|mps|cpu",issues:[{code,message,fix?}]}, models:[{id,label,rank,fits,batch_size,cached,download_gb,recommended,encode_per_s?,prep_per_s?,per_s?,estimated?,bottleneck?("calcolo|foto"),error?,eta_total_s,eta_pending_s}], recommendation:{model,device,batch_size,workers,reason}, library:{photos,videos,pending_photos,pending_videos,frames_total,frames_pending,video_frames}, benchmark:{at,stale}|null, benchmark_job_id|null, indexing_running}`. Rilevamento senza importare torch né aprire CUDA. `per_s` = min(calcolo, lettura foto); `estimated:true` = estrapolato da un modello misurato (costo `gflops` in `config.AVAILABLE_MODELS`); assente se non c'è nessuna misura.
- `POST /hardware/benchmark` `{models?:[id]}` (default: i modelli già scaricati) → job `hardware_bench` → `{job_id}`; 409 se l'indicizzazione gira (stessa GPU) o se la misura è già in corso; 400 se nessun modello scaricato. Sottoprocesso `python -m fotoscan.hardware bench`; risultati in `cache/hardware_bench.json` (legati a scheda video/CPU: se cambiano, `benchmark.stale`).

### routes_models.py (Impostazioni > Per esperti)
- Calibrazione per modello = `{temp_factor, search_thr:{min_match,min_gap,level_good,level_ok}}`. Effettiva = preset (`config.AVAILABLE_MODELS`, o `BASE_SEARCH_THR` / fattore 1 per i modelli non calibrati) + sostituzioni dell'utente (`Config.model_overrides`). La leggono `config.model_profile` → `Config.context_temperature()` (contesti) e `routes_search.search_thresholds()` (ricerca). Modelli aggiunti dall'utente: `Config.custom_models`, elencati anche in `/status.available_models` (`config.all_models`).
- `GET /models` → `{current, embedding_model, score_temperature, score_threshold, base_thresholds, limits, supported_types, reference_model, calibration_job_id|null, calibration_model|null, models:[{id,label,hint,tested,custom,calibrated,cached,current,in_catalog,preset,override|null,effective:{temp_factor,search_thr,source("preset|base|manuale|automatica"),temperature},proposal|null}]}`.
- `PUT /models/profile` `{id, temp_factor?, search_thr?:{...parziale}, source?:"automatica"}` → salva (400 fuori limiti o soglie non in ordine minima ≤ buona ≤ ottima; 404 modello sconosciuto). `DELETE /models/profile?id=` → calibrazione di fabbrica.
- `POST /models/custom` `{id:"autore/nome", label?}` → legge solo config.json da Hugging Face (`AutoConfig`): 400 se non esiste o se `model_type` non è in `config.SUPPORTED_MODEL_TYPES`; 409 se già presente. `DELETE /models/custom?id=` (409 se è il modello in uso; toglie anche la sua calibrazione).
- `POST /models/calibrate` `{id, n?}` → job `model_calibration` (sottoprocesso `python -m fotoscan.calibrate`): confronta il modello con SigLIP 2 Base sulle stesse foto (3000 con GPU, 1000 su CPU). Il risultato (`job.result`, e `proposal` in `GET /models`) è una proposta: si applica con `PUT /models/profile` + `source:"automatica"`. 409 se gira l'indicizzazione, una misura o un'altra calibrazione.

### routes_network.py (Impostazioni > Rete)
- Blocco della rete: `fotoscan/netguard.py`, installato all'import di `fotoscan` (server e sottoprocessi). Audit hook Python non rimovibile: solo loopback; rete aperta solo con `netguard.download_window()` verso Hugging Face/GitHub quando un modello manca (`netguard.load_pretrained` legge prima la cache con `local_files_only=True`). Ogni tentativo esterno va in `cache/rete.jsonl` (`FOTOSCAN_NETLOG` per i test).
- `GET /network[?limit=200]` → `{guard:{installed,window_open,window_reason,blocked_this_process,download_hosts,telemetry_env,log_file}, server:{host,port,local_only}, expected:[{id,name,hosts,purpose,when,sends,state,ok,managed}], summary:{blocked,allowed,last}, events:[{ts,outcome:"bloccato|consentito",host,port,event,purpose,library,caller,process,pid}]}` (eventi dal più recente).
- `POST /network/test` → `{blocked,message}`: tenta example.com:443 e verifica che venga fermato.
- `DELETE /network/log` → `{ok:true}`: svuota il registro.

### routes_update.py (Impostazioni > Aggiornamenti)
- Logica in `fotoscan/updater.py`; versione e repository in `fotoscan/version.py` (`__version__`, `UPDATE_REPO`, override `FOTOSCAN_UPDATE_REPO`). Rete solo con `netguard.download_window()` verso GitHub. Stato in `cache/updates/` (`state.json`, `pending.json`, `installed.json`, `staging/`, `backup/`).
- `GET /update` → `{current, repo, configured, git_checkout, settings:{update_check_on_start,update_auto_download,update_auto_install,update_prerelease}, checking, check:{checked_at,error,available,latest:{version,tag,name,notes,published_at,prerelease,html_url,zip_url}}|null, pending:{version,name,notes,approved,downloaded_at}|null, download_job:Job|null, last_install:{from,to,at,backup,requirements_changed,notes}|null, last_failure:{from,to,at,message,keepPackage}|null}`.
- `POST /update/check` → come `GET /update` (400 se il repository non è configurato). Con `update_auto_download` avvia anche il download.
- `POST /update/download` → `{job_id}` (job `update_download`, avanzamento in byte); 400 senza aggiornamento disponibile, 409 se la copia è un clone git.
- `POST /update/install` → approva l'installazione: la esegue `fotoscan.desktop` al prossimo avvio, prima di avviare il server (copia di riserva e ripristino se fallisce). La finestra pywebview espone `restart_app()` per riavviare subito.
- `DELETE /update/pending` → annulla il download in corso o elimina l'aggiornamento scaricato.
- Evento SSE `update` (stesso contenuto di `GET /update`). Il server controlla all'avvio (`fotoscan.server.__main__` → `routes_update.startup_check()`) se `update_check_on_start`.

### routes_cleanup.py (Pulizia)
- `GET /cleanup/summary`, `GET /cleanup/items` → candidati alla pulizia (file nel catalogo). `POST /cleanup/trash` `{ids:[...], confirm:true}` → sposta nel Cestino (Windows: Cestino; Linux: cestino freedesktop; mai cancellazione definitiva), toglie dal catalogo (a blocchi, con volti/persone/copertine) e ripara i gruppi duplicati. Risposta `{trashed, freed_bytes, errors:[{id,path,error}], missing}`: `missing` = file già spariti dal disco (tolti dal catalogo ma non contati in `trashed`). 409 se il piano è in ricalcolo o in applicazione.

### routes_nas.py (prefisso `/api/nas`)
- `GET /nas/cleanup` → anteprima dei file locali con copia verificata sul NAS: `{count, bytes, sample, ids:[...]}`.
- `POST /nas/cleanup` `{confirm:"CESTINO", expected_count, expected_ids:[...]}` → job. `expected_ids` deve coincidere con i candidati attuali (400 se elenco non valido, 409 se è cambiato dall'anteprima). Invio, ricezione e pulizia non girano mai insieme (409). `PUT /nas/settings` rifiuta cartelle NAS identiche o sovrapposte a sorgente e cartella di ricezione.

### routes_phone.py (prefisso `/api/phone`)
- `GET /phone/history`, `GET /phone/devices`, `GET /phone/folder`, `POST /phone/scan`, `POST /phone/import` (job). Errori di accesso al telefono → 503 con messaggio. Il risultato dell'import (`job.result`) contiene `aborted` (motivo, stringa vuota se completo) quando si ferma a metà, ad esempio telefono scollegato; lo storico lo registra come non riuscito.

### routes_places.py
- `GET /places?precision=1` → cluster GPS `[{lat,lon,count,sample_id,label:"41.9°N 12.5°E"}]` (griglia arrotondata, precision = decimali).
- `GET /places/files?lat=&lon=&precision=&offset=&limit=` → lista paginata.
- `GET /stats/overview` → `{per_year:[{year,photos,videos}], per_context:[{context,count}], per_kind, date_sources:[{source,count}], top_days:[{day,count}], size_total, size_by_kind}` per la pagina Statistiche.

### routes_people.py
- `GET /people?include_hidden=0` → `[]` se volti disattivati, altrimenti `[{id,name,count,cover_face_id,sample_file_id,hidden}]`.
- `GET /people/suggestions?limit=12&min_sim=0.38` → coppie di persone visibili con centroide simile `[{a, b, similarity}]` ordinate per somiglianza decrescente (esclude le coppie «Diverse»).
- `GET /people/{id}/files?offset=0&limit=120` → `{total, person:{...}, items:[File + {face_id}]}`.
- `PATCH /people/{id}` `{name?, hidden?, cover_face_id?}` → rinomina, nasconde/mostra o imposta la foto profilo personalizzata (`cover_locked=1`, sopravvive ai ricalcoli; `cover_face_id:null` ripristina scelta automatica).
- `POST /people/merge` `{ids:[...], into:id}` → unisce più persone in `into` (`locked=1`, ricalcola centroide). 400 se meno di due persone distinte, 404 se un id non esiste. Risposta `{merged, into, log_id}`. `hidden` della destinazione: resta nascosta solo se tutte le persone unite erano nascoste.
- `POST /people/merge/{log_id}/undo` → annulla l'unione (solo la più recente non annullata; altrimenti 409). Ripristina persone, volti e stato della destinazione.
- `POST /people/split` `{person_id, face_ids:[...]}` → sposta i volti selezionati in una nuova persona (`locked=1`). Risposta `{person_id, from, moved}`. 400 se i volti non appartengono alla persona.
- `GET /people/suggestion-groups?limit=12&min_sim=0.38` → gruppi `[{members:[persona...], similarity}]` (componenti connesse di persone simili; mai fusi due membri segnalati «Diverse»).
- `GET /people/not-same` → `[{a, b}]`. `POST /people/not-same` `{a, b}` → segna «Diverse». `DELETE /people/not-same/{a}/{b}` → annulla.
- `POST /faces/assign` `{face_ids:[...], person_id:id|null|"new"}` → riassegna o sgancia volti (`locked=1`).
- `POST /faces/{id}/exclude` / `POST /faces/{id}/restore` → esclude un falso volto o lo ripristina.
- `GET /faces/{id}/crop?size=256` → ritaglio JPEG quadrato del volto (cache `cache/faces/`).
- `GET /people/job` → `{job: {...}|null}` job volti attivo (`volti_scan`, `volti_cluster`, `volti_modelli`) per sincronizzazione cross-pagina.
- `POST /people/scan` `{reset?:bool}` → job SSE che rileva i volti mancanti e aggiorna i gruppi di persone (409 se indicizzazione o altro job volti attivo).
- `POST /people/reset-scan` → azzera `faces_done` e i volti non bloccati/non esclusi per rianalizzare con la nuova `faces_det_size`, mantenendo persone con nome, `locked=1` ed `excluded=1` (409 se occupato).
- `POST /people/recluster` → job SSE che ricalcola i gruppi dei volti non bloccati.
- `POST /people/download-models` → job SSE che scarica una tantum i modelli ONNX `buffalo_l` in `cache/models/faces/` con verifica SHA-256.

## 3. Frontend — convenzioni
- **ES modules, niente build.** Ogni view: `export async function mount(root, ctx)` con `ctx = {params, query, navigate, signal}`; ritorna una funzione di cleanup (rimuove listener/SSE/observer). `root` è l'elemento già pulito. Router a hash (`#/gallery?filter=video`), vedi `js/routes.js`.
- **Solo componenti `md-*` di @material/web** (già registrati da `vendor/material-web.js`) + token CSS `--md-sys-color-*`, `--shape-*`, `--elev-*`, `--sp-*`, `--dur-*`. **Vietato** colori hard-coded (`#fff`, `rgb(...)` per superfici/testo), vietati altri framework UI. Stati: hover/focus/pressed ottenuti con layer di stato M3 (opacità 8/10/10%) o dai componenti. Focus ring visibile, `aria-label` su ogni icon-button, ordine di tab logico, `prefers-reduced-motion` rispettato.
- **Componenti disponibili nel bundle** (@material/web 2.5.0, `vendor/material-web.js`): core (button, icon-button, fab, chip-set/filter/assist/suggestion/input, dialog, menu, list, tabs, text/select field, slider, switch, radio, checkbox, progress, divider, icon, elevation, focus-ring) + labs: `md-navigation-bar`/`md-navigation-tab`, `md-navigation-drawer` (+ `-modal`), `md-outlined-segmented-button(-set)`, `md-badge`, `md-elevated/filled/outlined-card`, `md-item`. **Non esistono** nella versione installata: top app bar, search bar, bottom sheet, carousel, date/time picker, navigation rail, side sheet: restano markup proprio (`css/shell.css`, `css/components.css`) ispirato a M3. Dialog e snackbar sono in `js/ui/` con stili in `components.css`.
- **Layout adattivo M3** (window size classes): compatto `<600px` (bottom bar 5 voci primarie, 1 colonna), medio `600–839px` (navigation rail), esteso `≥840px` (rail espansa/drawer permanente 360px opzionale). Margini 16px compatto / 24px medio-esteso. Le view usano classi `.view`, `.view-header`, `.section`, `.grid-auto`, `.card`, definite in `css/base.css` e `css/components.css`.
- **Tipografia**: classi `md-typescale-*` (display/headline/title/body/label × large/medium/small) già disponibili dal bundle. Icone: `icon('nome')` da `js/ui/el.js` (Material Symbols Outlined).
- **Dati**: sempre via `api.get/post/put/del` (`js/api.js`); immagini via `fileUrl.thumb(id,size)` ecc.; stati di caricamento con **skeleton** (non spinner a pagina intera), stato vuoto con `emptyState`, errori con `showSnackbar`. Liste lunghe sempre **paginate/virtualizzate** (l'archivio ha 14.245+ file).
- **Stato globale**: `store` (`status`, `settings`, `theme`). Eventi realtime: `onEvent('index'|'job'|'connection', fn)`.
- **Prestazioni**: nessun layout thrashing; `loading="lazy"` + `decoding="async"` su `<img>`; `IntersectionObserver`; debounce sulle ricerche (250 ms); budget: prima schermata utile < 1 s su cache calda.
- **Testo**: italiano corretto (accenti), tono semplice, niente gergo tecnico nelle schermate (es. "Quasi uguali" invece di "near-duplicate").

### Interfacce condivise (firme FISSE: chi le usa le importa; chi le possiede le implementa esattamente così)
| Modulo | Export |
|---|---|
| `js/ui/snackbar.js` | `showSnackbar(message, {actionLabel?, onAction?, duration=4000, error=false})` |
| `js/ui/dialog.js` | `confirmDialog({title, message, confirmLabel='Conferma', cancelLabel='Annulla', danger=false}) → Promise<boolean>` · `promptDialog({title, label, value='', confirmLabel='OK', placeholder?, suggestions?:[str]}) → Promise<string\|null>` · `choiceDialog({title, message?, options:[{value,label,description?}], confirmLabel}) → Promise<value\|null>` · `albumPickerDialog({fileIds}) → Promise<boolean>` (scelta/creazione album e aggiunta) |
| `js/ui/skeleton.js` | `skeletonGrid(count=24, {tile=180})` · `skeletonList(count=6)` · `skeletonCard()` → `HTMLElement` |
| `js/ui/empty-state.js` | `emptyState({icon, title, message?, actionLabel?, onAction?}) → HTMLElement` |
| `js/ui/photo-tile.js` | `createTile(file, {size=180, selectable=false, selected=false, showMeta=false, onOpen?(file,tile), onSelectToggle?(file,selected,tile)}) → HTMLElement` (miniatura quadrata, badge video con durata, cuore preferito su hover/selezione, spunta di selezione, `tile.dataset.id`) · `updateTile(tile, patch)` |
| `js/ui/virtual-grid.js` | `class VirtualGrid { constructor(scrollEl, {fetchPage(offset,limit)→Promise<{total,items}>, renderItem(file,index)→HTMLElement, tile=180, gap=4, pageSize=120, groups?:[{label,count,offset}] , renderHeader?(group)→HTMLElement}); async reload(); scrollToOffset(n); getItems(); destroy(); onVisibleRange?(first,last) }` |
| `js/ui/scrubber.js` | `createScrubber(host, {buckets:[{key,label,count,offset}], onSeek(offset), getScrollRatio?}) → {update(offsetVisibile), destroy()}` |
| `js/ui/chips.js` | `filterChips([{id,label,icon?}], {selected, onChange(id)}) → HTMLElement` (md-chip-set a selezione singola) |
| `js/ui/selection.js` | `createSelection() → {ids:Set, toggle(id), clear(), has(id), count, subscribe(fn)}` + `selectionBar(host, {selection, actions:[{id,icon,label,run(ids)}]})` |
| `js/views/viewer.js` | `openViewer({items:[File], index, loadMore?:async()=>[File], onClose?, onChange?(file)})` overlay a schermo intero (non è una route): zoom/pan, swipe, frecce da tastiera, ESC, pannello info, preferito, aggiungi ad album, video con controlli, precarica adiacenti. |
| `js/shell.js` | `mountShell(appEl) → Promise<HTMLElement>` (ritorna l'outlet `.view-outlet`) |

## 4. Design Material 3 — checklist che ogni view deve rispettare
Colore dai token (superfici `surface-container-*`, accenti `primary/secondary/tertiary`, errori `error`), forma (tile `--shape-sm`, card `--shape-md`, dialog `--shape-xl`), elevazione tonale (preferita all'ombra), top app bar (small/medium collapsing), FAB solo dove c'è un'azione primaria, dialog/sheet/menu/snackbar M3, chip per filtri, segmented button per scelte esclusive brevi, progress lineare/circolare M3, switch/slider/select/text field M3, ripple, transizioni container-transform leggere (`--dur-medium`), tema chiaro/scuro/sistema.

## 5. Avvio produzione
`python -m fotoscan.desktop` avvia `python -m fotoscan.server` (uvicorn) su porta libera e apre Edge in modalità app. (Sostituito dal launcher in `fotoscan/desktop.py` a fine lavori.)

## 6. Proprietà dei file (un file = un solo proprietario)
**Backend** (`fotoscan/server/`): `routes_status.py`, `routes_files.py`, `routes_media.py`, `routes_search.py`, `routes_plan.py`, `routes_duplicates.py`, `routes_index.py`, `routes_apply.py`, `routes_library.py`, `routes_places.py` — uno per agente. Core (`app.py`, `deps.py`, `jobs.py`) è dell'orchestratore.
**CSS**: `css/base.css` (reset, layout adattivo, classi `.view*`, `.section`, `.grid-auto`, scrollbar, focus), `css/components.css` (tile, card, skeleton shimmer, scrubber, viewer overlay, selection bar, tabelle) — uno per agente; token in `tokens*.css` (orchestratore).
**UI kit** (`js/ui/`): `snackbar.js`, `dialog.js`, `skeleton.js`+`empty-state.js`, `photo-tile.js`, `virtual-grid.js`, `scrubber.js`, `chips.js`+`selection.js` — uno per agente.
**Shell**: `js/shell.js` (+ eventuale `css/shell.css` collegato da shell.js via `<link>` dinamico).
**View** (`js/views/`): `home.js`, `gallery.js`, `viewer.js`, `search.js`, `folders.js`, `folder-detail.js`, `duplicates.js`, `albums.js`, `album-detail.js`, `places.js`, `stats.js`, `indexing.js`, `apply.js`, `settings.js` (+ `settings-general.js`, `settings-rules.js`, `settings-labels.js`: ciascuno `export function mountSection(container, {settings, save})`), `onboarding.js`, `help.js` — uno per agente. Una view può avere un proprio CSS `css/view-<nome>.css` caricato dinamicamente dalla view stessa.
