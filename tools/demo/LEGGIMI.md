# Libreria demo, screenshot e video

Gli screenshot in [`docs/screenshots/`](../../docs/screenshots/) sono stati creati su una **libreria dimostrativa**:
non contengono foto, nomi, percorsi o luoghi reali.

## Da dove vengono i contenuti
- **Foto:** immagini di [Unsplash](https://unsplash.com/license) scaricate da picsum.photos. La licenza Unsplash consente l'uso gratuito, anche commerciale, senza attribuzione obbligatoria; i crediti sono comunque in [`docs/screenshots/crediti-foto-unsplash.json`](../../docs/screenshots/crediti-foto-unsplash.json).
- **Inventati:** date, coordinate GPS, viaggi, screenshot di telefono, scontrini, video e album. Lo scontrino usa "Via Esempio 1" e una partita IVA fatta di zeri.

## Nota sulle soglie di ricerca
Le foto Unsplash sono più "artistiche" delle foto di famiglia e ottengono punteggi più bassi. Nella libreria demo le soglie di SigLIP 2 SO400M sono state abbassate da Impostazioni > Per esperti (minima 0,055, stacco 0,05, buona 0,065, ottima 0,08). Le frasi senza senso continuano a dare 0 risultati.

## Rigenerare
Script in questa cartella, nell'ordine:
1. `fetch.py`: scarica le foto.
2. `classify.py`: le divide per tema.
3. `build.py`: crea la libreria in `C:\FotoScanDemo\Foto`.
4. `server.sh`: avvia l'app isolata sulla porta 9300.
5. Analisi e piano dall'app.
6. `shots.py`: crea gli screenshot.
7. `record.py` + ffmpeg: crea il video (concatenazione dei fotogrammi con le durate di `frames.txt`).
