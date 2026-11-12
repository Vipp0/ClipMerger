# ClipMerger

Applicazione desktop per Windows che unisce automaticamente una **sigla iniziale** e una **sigla finale** a un batch di episodi (cartoni animati, programmi TV, ecc.), producendo per ciascun video un nuovo file: `sigla iniziale + episodio + sigla finale`. Basta anche una sola delle due sigle.

## Funzionalità

- **Coda batch** con drag & drop di una cartella intera (o di singoli file video) e barra di avanzamento per ogni video. Si possono rimuovere più file alla volta: selezione con Ctrl/Maiusc (Ctrl+A per tutti), poi Canc o tasto destro.
- **Riconoscimento automatico delle sigle**: se nella cartella trascinata un file contiene "sigla" insieme a "iniziale" o "finale" nel nome (anche separate da parentesi o altro testo), viene assegnato da solo ai campi corrispondenti invece di finire in coda come episodio.
- **Ricodifica robusta**: episodio, sigla iniziale e sigla finale possono avere risoluzione, framerate o codec diversi tra loro — l'episodio fa sempre da riferimento e le sigle vengono adattate.
- **Codec**: H.264, H.265 (HEVC), AV1, con rilevamento automatico e uso opzionale dell'accelerazione hardware (NVIDIA NVENC, Intel QuickSync, AMD AMF) se disponibile.
- **Codec "Come originale"** (predefinito): ogni episodio esce nello stesso codec che ha già (H.264, H.265 o AV1; per altri formati si usa H.265). Appena carichi i file, sopra la scelta del codec compare cosa sono gli originali (codec e bitrate) e, se scegli un codec meno efficiente con il bitrate legato all'originale, un avviso con il bitrate consigliato per compensare (esempio: da AV1 a H.264 servono circa il doppio dei kbit/s).
- **Controllo qualità/peso**: qualità costante (CRF), bitrate costante (CBR), oppure bitrate allineato all'originale per minimizzare la perdita percepita.
- **Ottimizzazione per cartoni animati** (`tune animation`, solo codifica software H.264/H.265).
- Gestione di **tracce audio multiple** e **sottotitoli** dell'episodio, mantenuti sincronizzati nel file finale, con **nomi delle tracce**, lingua, traccia predefinita e flag "forzati" identici all'originale (in MP4 i nomi sono salvati nel campo `handler_name`).
- **Audio a scelta**: come originale (stesso codec, canali e bitrate dell'episodio, con ripiego automatico su AAC se il contenitore o ffmpeg non lo supportano), AAC con i canali originali (5.1 resta 5.1), AAC stereo 192k, oppure FLAC senza perdita.
- **Capitoli** dell'episodio mantenuti e riallineati alla durata della sigla iniziale; **titolo interno del file** preso dall'episodio.
- **Allegati e copertine** dell'episodio portati nel file finale: font dei sottotitoli e copertine in MKV, copertina incorporata in MP4 (un MP4 non può contenere font allegati).
- **Sincronia audio/video** garantita attorno alle sigle anche con file "sporchi" (audio più lungo o in ritardo rispetto al video): video e audio si concatenano insieme.
- **Video ruotati** (metadato di rotazione, tipico dei video da telefono) gestiti correttamente.
- **Deinterlaccia automatica** dei video segnalati come interlacciati (vecchi DVD/TV), disattivabile; **10 bit mantenuti** per H.265/AV1 software se l'episodio è a 10 bit.
- **Contenitore di ripiego**: con "come originale", se l'estensione dell'episodio non può contenere il codec scelto (es. `.webm`, `.mpg`, o `.avi` con H.265) il file viene salvato come `.mkv`; nomi di output duplicati vengono numerati. I sottotitoli non supportati dal formato di uscita vengono saltati (con nota nello stato) invece di far fallire il file.
- **MP4/MOV pronti per lo streaming** (Plex, NAS, web): l'indice del file viene scritto all'inizio (`faststart`), così la riproduzione parte subito.
- **A coda finita**: un selettore in alto a destra (valido per entrambe le schede) permette di scegliere «Non fare niente» o «Chiudi il programma»: con la seconda, quando la coda finisce da sola parte un conto alla rovescia di 10 secondi in cui si può annullare. Non scatta se si ferma l'elaborazione a mano, e a ogni avvio torna su «Non fare niente».
- **Chiusura sicura**: chiudere la finestra durante la codifica chiede conferma, ferma ffmpeg ed elimina i file parziali.
- **Proporzione dei pixel (SAR)** dell'episodio preservata, così i video "anamorfici" (vecchi DVD/AVI, TV registrata) non risultano più stretti.
- **Verifica di integrità** automatica dell'output (controllo durata) al termine di ogni file.
- **Codifica a 2 passaggi** (bitrate medio) per software H.264/H.265/AV1: stesso bitrate target, qualità distribuita meglio tra le scene.
- **Tempo rimanente stimato**, per singolo file e per l'intero batch.
- **Riepilogo di controllo** (durate, risoluzioni, avvisi) prima di avviare il batch, con possibilità di annullare. L'analisi dei file avviene in background con un contatore ("Analisi dei file: 3/25") e si può interrompere.
- **Dettaglio errore consultabile**: doppio click su una riga in coda per vedere lo stato/errore completo.
- **Controllo aggiornamenti** automatico rispetto all'ultima release GitHub.
- Elaborazioni **parallele** configurabili, con possibilità di annullare il batch in corso.
- Tema **chiaro / scuro / automatico** (segue le impostazioni di Windows).

- **Scheda "Sostituisci sigla"**: toglie la sigla che c'è già dentro gli episodi (iniziale e/o finale, riconosciute dall'audio: la "impara" dagli episodi o la cerca da un file di esempio) e ci mette una nuova, audio e video. Si vede una tabella con i risultati prima di scrivere qualsiasi cosa, c'è l'anteprima delle giunzioni, e ogni episodio viene ricodificato una volta sola nel suo codec. Una schermata col titolo dopo la sigla viene conservata (senza la musica; si può spegnere con la casella apposita) e i punti di taglio si possono correggere a mano nella tabella.
- **Scheda "Modifica tracce"**: cambia ordine, traccia predefinita, nome, lingua e presenza delle tracce audio e dei sottotitoli dei video di una cartella (o della coda di «Unisci sigle»), **senza ricodificare** (si riscrive solo il contenitore: pochi secondi per episodio, audio e video identici). Regole valide per tutti i file ("metti per prima e rendi predefinita la traccia ita", "elimina i sottotitoli eng", "tieni solo ita, eng", "rinomina le tracce ita in Italiano", "assegna la lingua a quelle senza lingua", "segna / togli / elimina le forzate", "nessuna predefinita") con la tabella prima/dopo, e modifica a mano di un singolo video. Il risultato è un file nuovo in una cartella a parte; MP4 tiene solo sottotitoli di testo, i formati vecchi (avi, ts...) escono come MKV.
- **Scheda "Dividi puntate"**: taglia i file che contengono tante puntate (o sketch) in un file per puntata, con il nome `1x012 Titolo.mkv`. Primo profilo: «Sketch senza sigle» (Camera Café): ogni sketch comincia con lo stesso identico filmato (il bicchiere), che il programma riconosce confrontando i fotogrammi con un campione (`assets/profili/camera_cafe.npz`); il taglio cade sullo stacco netto che lo precede, quindi è preciso al fotogramma. Si scartano l'apertura iniziale e i titoli di coda arancioni, e il primo pezzo tiene anche il breve inizio senza titolo. Il titolo scritto sullo schermo viene letto con il lettore di testo integrato in Windows (italiano) e confrontato con la lista di TMDB, che dà anche il numero (serie e stagione si scelgono nella scheda; la stagione si ricava dal nome dei file, es. S01E01). La lista di una stagione si scarica una volta con la chiave personale di TMDB (campo nella scheda, resta nelle impostazioni del programma) e si tiene sul PC in `%LOCALAPPDATA%\ClipMerger\tmdb`. La tabella mostra tutti i pezzi con l'immagine del titolo: i dubbi sono evidenziati e non spuntati, tempi, numero e titolo si correggono a mano (scrivendo un titolo che è nella lista, il numero si aggiorna). Ogni pezzo è ricodificato una sola volta (H.264 come l'originale o H.265; qualità CRF 18/20/23, bitrate mai sopra quello dell'originale, audio com'è); i file originali non si toccano.
- **File di log**: il programma scrive `%LOCALAPPDATA%\ClipMerger\logs\clipmerger.log` (impostazioni di ogni lavoro, comando di ogni ffmpeg, errori completi, memoria del computer, del programma e degli ffmpeg in corso ogni 30 secondi, picco di memoria di ogni codifica) e `crash.log` (blocchi fatali di Python/Qt). Se la sessione precedente non si era chiusa normalmente (blocco, chiusura forzata, PC spento) lo segnala all'avvio. Il pulsante «Apri i log» in alto a destra apre la cartella. Il log ruota da solo (restano circa 3 MB) e contiene solo nomi di file, impostazioni ed errori.

## Requisiti

- Windows
- [ffmpeg e ffprobe](https://ffmpeg.org/download.html) installati e presenti nel PATH di sistema
- Per l'uso quotidiano: solo l'eseguibile compilato (vedi [Releases](../../releases)), non serve Python

## Avvio da sorgente (sviluppo)

```bash
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
python main.py
```

## Compilazione dell'eseguibile

```bash
build.bat
```

Produce l'eseguibile in `dist\ClipMerger\ClipMerger.exe` (modalità PyInstaller `--onedir`: eseguibile + cartella `_internal` con le dipendenze, da tenere insieme).

## Roadmap (idee per versioni future)

- **Scheda "Sostituisci sigla"** (in lavorazione, a fasi): trova automaticamente una sigla dentro gli episodi (da un campione, oppure "imparandola" dalla cartella) e la sostituisce con un'altra, solo audio o audio + video. Piano completo e stato delle fasi in [docs/PIANO_SOSTITUISCI_SIGLA.md](docs/PIANO_SOSTITUISCI_SIGLA.md).
- Altre azioni a coda finita (spegni il computer, sospendi), con lo stesso conto alla rovescia.
- Preset di codifica salvabili/richiamabili, in stile HandBrake (al posto del semplice "ricorda le ultime impostazioni").
- Preset più veloce dedicato al primo passaggio della codifica a 2 passaggi, per ridurre il tempo totale (oggi entrambi i passaggi usano lo stesso preset, quindi il 2-pass costa quasi il doppio del tempo di un singolo passaggio).

## Limiti noti

- **Materiale HDR**: viene ricodificato senza conversione della gamma di colori (il 10 bit si conserva, ma i metadati HDR no).
- **Interlacciamento non dichiarato**: viene riconosciuto solo se il file lo segnala (`field_order`); un video interlacciato senza flag non viene deinterlacciato.
- **Con la scheda video** (non verificato qui, nessuna NVIDIA/AMD disponibile): un numero di elaborazioni parallele superiore al limite delle sessioni NVENC può far fallire alcuni file, e su AMD il preset scelto non ha effetto (AMF non usa `-preset`).
- **Copertine in MP4** conservate come immagine incorporata; i font allegati dei MKV non possono stare in un MP4.
- **Sottotitoli a immagine** (PGS/VobSub) si copiano solo in MKV; verso altri formati vengono saltati e lo stato della riga lo segnala.

## Struttura del progetto

```
main.py             GUI (PySide6), con le due schede
tab_sostituisci.py  Scheda "Sostituisci sigla"
sostituisci.py      Sostituzione delle sigle dentro un episodio: punti di taglio, comando ffmpeg
trova_sigla.py      Riconoscimento delle sigle dall'audio (numpy)
applog.py           File di log, memoria usata, rapporto dei blocchi
tab_tracce.py       Scheda "Modifica tracce"
tab_dividi.py       Scheda "Dividi puntate"
dividi.py           Riconosce i bicchieri, trova i tagli e ricodifica i pezzi
titoli.py           Lista dei titoli da TMDB e confronto con i titoli letti
ocr_windows.py      Lettura del testo dalle immagini (lettore integrato in Windows)
tracce.py           Lettura, regole e riscrittura (remux) delle tracce audio/sottotitoli
fine_coda.py        Conto alla rovescia di "a coda finita"
merger.py           Logica di unione video: probing, comando ffmpeg, verifica integrità
utils.py            Rilevamento ffmpeg/ffprobe, encoder hardware, utility varie
assets/             Icone usate dall'interfaccia
requirements.txt    Dipendenze Python
ClipMerger.spec     Configurazione build PyInstaller
build.bat           Script di compilazione
```

## Riconoscimenti

Questo prodotto usa le API di TMDB ma non è approvato né certificato da TMDB.
