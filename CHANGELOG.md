# Novità di PhotoScan

Ogni versione ha una sezione `## <versione>`. Il workflow `.github/workflows/release.yml` usa la sezione
della versione taggata come testo della Release, e PhotoScan la mostra in Impostazioni > Aggiornamenti
prima dell'installazione. Per una versione senza sezione la Release usa le note generate da GitHub.

## 0.1.11

Avvio, aggiornamenti e pacchetti.

- Aggiornamenti molto più leggeri su Windows: PyTorch, torchvision e transformers escono dal pacchetto dell'app e diventano un "runtime" a parte, scaricato solo quando cambia la sua versione. Un aggiornamento normale pesa poche centinaia di MB invece di alcuni GB. Le versioni fino alla 0.1.10 continuano ad aggiornarsi dal pacchetto completo, che resta in ogni Release.
- Ogni Release pubblica `SHA256SUMS`: l'aggiornamento e `PhotoScan-Setup` verificano ogni file prima di estrarlo. Le parti `.001/.002` incomplete (Release ancora in caricamento) non vengono più offerte e i download riprendono dopo un'interruzione; i file `.part` non restano più sul disco.
- La Release nasce come bozza e diventa pubblica solo dopo il caricamento di tutti i file e dopo le prove di fumo (l'exe importa PyTorch e transformers, anche col runtime a parte).
- «Installa automaticamente» ora funziona anche nell'eseguibile Windows e nell'AppImage (prima l'aggiornamento scaricato restava fermo).
- Impostazioni > Aggiornamenti: «Altre versioni» (elenco delle Release con note, «Scarica installer» e «Installa questa versione», anche più vecchia: prima del downgrade si salva una copia del catalogo) e «Disinstalla».
- Disinstallazione: PhotoScan compare in «App installate» di Windows (senza amministratore). Il disinstallatore chiude l'app, mostra cosa toglie e quanto spazio libera, e conserva di default catalogo, cache e impostazioni. Non tocca mai le cartelle delle foto. Su Linux toglie il collegamento nel menu e, a scelta, i dati.
- `PhotoScan-Setup` accetta `--version`, `--silent` e `--uninstall`, sceglie la versione dalla finestra e non distrugge più l'installazione esistente se PhotoScan è aperto (la vecchia cartella torna al suo posto).
- Il collegamento sul Desktop e nel menu Start si crea anche con nomi utente con apostrofi o accenti e con il Desktop spostato da OneDrive; se non si riesce lo dice.
- Avvio: due doppi clic ravvicinati non lanciano più due server sullo stesso catalogo; la finestra di attesa compare se l'avvio è lento; il server si spegne con l'ultima finestra chiusa; il processo del server non viene più terminato se il suo numero è stato riutilizzato da un altro programma; la porta resta la stessa a ogni avvio; WebView2 assente o rotto ripiega su Edge/Chrome; preferenze e «Più tardi» del banner restano tra un avvio e l'altro.
- Linux: Chromium e zenity partono anche dall'AppImage (variabile `LD_LIBRARY_PATH` ripulita); senza browser né zenity il server non si spegne subito; l'AppImage si sostituisce anche su un altro disco; «Installa e riavvia» riapre l'app.
- Aggiornamento di `PhotoScan.exe`: messaggio chiaro se la cartella non è scrivibile (Program Files) o se Windows blocca lo script PowerShell; il runtime vecchio si toglie solo dopo un avvio riuscito.
- `importazione dal telefono` funziona di nuovo nell'exe (`phone_mtp.ps1` mancava nel pacchetto).
- Confronto delle versioni corretto con quattro numeri e con etichette di prova (alpha, beta, rc).
- Dipendenze bloccate alle versioni provate (`requirements.txt`, `requirements-runtime.txt`).

Sicurezza e protezione dei file.

- Il server risponde solo a richieste dirette a questo PC (`127.0.0.1`, `localhost`): una pagina web aperta nel browser non può più leggere le foto né comandare l'app.
- Nomi di album, persone ed etichette non vengono più interpretati come HTML nei messaggi.
- Cestino sicuro su Windows e Linux: se un file non può andare nel Cestino (Cestino disattivato o pieno, disco di rete) PhotoScan si ferma e lo dice, invece di cancellarlo per sempre.
- Duplicati: se l'originale di un gruppo sparisce, al suo posto va una copia identica e non una foto solo simile; «Cestina copie» salta i gruppi il cui originale non è più sul disco; la scelta manuale dell'originale non lascia più spostamenti sbagliati dopo un ricalcolo.
- Importazione dal telefono: i file copiati a metà (cavo staccato, annullamento) non finiscono più nell'archivio; foto diverse con lo stesso nome non vengono più scartate né duplicate a ogni importazione; un telefono scollegato non blocca più l'importazione per ore.
- NAS: invio e pulizia rifiutano una cartella NAS uguale alla sorgente; la pulizia conferma l'elenco esatto dei file mostrato in anteprima; operazioni di rete con tempo massimo.
- Crea cartelle: mai sovrascrivere, nomi riservati di Windows e nomi troppo lunghi gestiti, controllo dello spazio e dei collegamenti prima di iniziare, arresto a disco pieno.

Correzioni.

- La data di ripresa dei video viene letta correttamente (prima veniva usata la data del file): alla prossima analisi PhotoScan la ricalcola per i video già presenti.
- Senza ffmpeg, con un disco scollegato o con un formato non supportato i file non vengono più nascosti per sempre come «danneggiati»; «Rimuovi danneggiati» ricontrolla ogni file prima di toglierlo.
- Persone: un errore della GPU o di lettura non segna più le foto come «analizzate senza volti»; «Azzera scansione volti» conserva i nomi; avatar sempre del volto giusto; ripiego automatico sulla CPU; «Annulla esclusione» riporta il volto alla sua persona.
- Analisi: ripiego sulla CPU se la GPU non funziona, lotto ridotto se la memoria finisce, disco scollegato senza errori permanenti, cartelle di servizio dei NAS (`@eaDir`, `#recycle`…) ignorate, percorsi lunghi su Windows, «Ferma» su Linux, messaggio chiaro se il modello cambia.
- Un NAS o un disco di rete spento non blocca più l'interfaccia; meno errori «database is locked» durante l'analisi.
- Link «Vedi dettagli», «Vedi nei duplicati» e fine del primo avvio aprono la pagina giusta; doppio clic sul Cestino innocuo; l'app aspetta il server anche se parte lentamente.
- `config.json` danneggiato salvato come `config.json.bad` invece di essere perso; salvataggi atomici.

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
