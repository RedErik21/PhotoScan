# Novità di PhotoScan

Ogni versione ha una sezione `## <versione>`. Il workflow `.github/workflows/release.yml` usa la sezione
della versione taggata come testo della Release, e PhotoScan la mostra in Impostazioni > Aggiornamenti
prima dell'installazione. Per una versione senza sezione la Release usa le note generate da GitHub.

## 0.1.10

- Indicizzazione nell'eseguibile Windows corretta: l'errore "operator torchvision::nms does not exist" su AutoProcessor è risolto.
- Build Linux (AppImage) funzionante.
- Risorsa di versione di `PhotoScan.exe` solo su Windows.

## 0.1.8

Non pubblicata da sola: la build Windows e Linux di questa versione è fallita (vedi 0.1.9). Il contenuto è nella 0.1.9.

- NAS: invio e ricezione verificati con hash, pulizia nel Cestino, rifiuto dei dischi di rete.
- Ricerca: modello caricato al primo uso e scaricato dopo 10 minuti di inattività.
- Video: correzione dei fotogrammi uguali nei video lunghi.
- Server: cache di stato, limiti su job e cache in memoria, errori interni non esposti.
- Database: schema e migrazioni eseguite una volta per catalogo, indici aggiuntivi.
- Frontend: polling fermo con finestra nascosta, SSE con backoff, asset in cache.
- Indicizzazione: embedding letti una volta, benchmark con il batch reale.
- Opzioni avanzate: raggruppamento volti approssimato (spento di default).

## 0.1.7

- Persone: gruppi di suggerimento, segnala «Diverse», unione annullabile, divisione di una persona.
- Indicizzazione: stato con codice di errore, ultimo ribasamento e sorgente raggiungibile.
- Disco di rete: rilevamento della raggiungibilità.

## 0.1.6

- Linux: finestra con l'icona di PhotoScan e voce nel menu applicazioni.
- Sito: download diretto per Linux (AppImage).

## 0.1.5

- Release automatica per Windows e Linux.
- Aggiornamento: scelta della versione CPU o GPU.
- Test automatici di installazione e aggiornamento.

## 0.1.4

- Aggiornamento automatico di `PhotoScan.exe`: download, installazione alla chiusura e ripristino se il pacchetto è rotto.
- Nome del prodotto unificato in «PhotoScan».
- Nuova sezione Aiuto.
- Launcher di installazione che sceglie CPU o GPU in base all'hardware.

## 0.1.3

- Banner degli aggiornamenti in fondo alla pagina: download con avanzamento, installa al riavvio, «Più tardi».
- Aggiornamenti dal repository GitHub ufficiale.
