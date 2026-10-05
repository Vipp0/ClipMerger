# ClipMerger

Applicazione desktop per Windows che unisce automaticamente una **sigla iniziale** e una **sigla finale** a un batch di episodi (cartoni animati, programmi TV, ecc.), producendo per ciascun video un nuovo file: `sigla iniziale + episodio + sigla finale`. Basta anche una sola delle due sigle.

## Funzionalità

- **Coda batch** con drag & drop di una cartella intera (o di singoli file video) e barra di avanzamento per ogni video. Si possono rimuovere più file alla volta: selezione con Ctrl/Maiusc (Ctrl+A per tutti), poi Canc o tasto destro.
- **Riconoscimento automatico delle sigle**: se nella cartella trascinata un file contiene "sigla" insieme a "iniziale" o "finale" nel nome (anche separate da parentesi o altro testo), viene assegnato da solo ai campi corrispondenti invece di finire in coda come episodio.
- **Ricodifica robusta**: episodio, sigla iniziale e sigla finale possono avere risoluzione, framerate o codec diversi tra loro — l'episodio fa sempre da riferimento e le sigle vengono adattate.
- **Codec**: H.264, H.265 (HEVC), AV1, con rilevamento automatico e uso opzionale dell'accelerazione hardware (NVIDIA NVENC, Intel QuickSync, AMD AMF) se disponibile.
- **Controllo qualità/peso**: qualità costante (CRF), bitrate costante (CBR), oppure bitrate allineato all'originale per minimizzare la perdita percepita.
- **Ottimizzazione per cartoni animati** (`tune animation`, solo codifica software H.264/H.265).
- Gestione di **tracce audio multiple** e **sottotitoli** dell'episodio, mantenuti sincronizzati nel file finale, con **nomi delle tracce**, lingua, traccia predefinita e flag "forzati" identici all'originale (in MP4 i nomi sono salvati nel campo `handler_name`).
- **Audio a scelta**: come originale (stesso codec, canali e bitrate dell'episodio, con ripiego automatico su AAC se il contenitore o ffmpeg non lo supportano), AAC con i canali originali (5.1 resta 5.1), AAC stereo 192k, oppure FLAC senza perdita.
- **Capitoli** dell'episodio mantenuti e riallineati alla durata della sigla iniziale; **titolo interno del file** preso dall'episodio.
- **Allegati e copertine** dell'episodio portati nel file finale: font dei sottotitoli e copertine in MKV, copertina incorporata in MP4 (un MP4 non può contenere font allegati).
- **Proporzione dei pixel (SAR)** dell'episodio preservata, così i video "anamorfici" (vecchi DVD/AVI, TV registrata) non risultano più stretti.
- **Verifica di integrità** automatica dell'output (controllo durata) al termine di ogni file.
- **Codifica a 2 passaggi** (bitrate medio) per software H.264/H.265/AV1: stesso bitrate target, qualità distribuita meglio tra le scene.
- **Tempo rimanente stimato**, per singolo file e per l'intero batch.
- **Riepilogo di controllo** (durate, risoluzioni, avvisi) prima di avviare il batch, con possibilità di annullare. L'analisi dei file avviene in background con un contatore ("Analisi dei file: 3/25") e si può interrompere.
- **Dettaglio errore consultabile**: doppio click su una riga in coda per vedere lo stato/errore completo.
- **Controllo aggiornamenti** automatico rispetto all'ultima release GitHub.
- Elaborazioni **parallele** configurabili, con possibilità di annullare il batch in corso.
- Tema **chiaro / scuro / automatico** (segue le impostazioni di Windows).

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

- Preset di codifica salvabili/richiamabili, in stile HandBrake (al posto del semplice "ricorda le ultime impostazioni").
- Preset più veloce dedicato al primo passaggio della codifica a 2 passaggi, per ridurre il tempo totale (oggi entrambi i passaggi usano lo stesso preset, quindi il 2-pass costa quasi il doppio del tempo di un singolo passaggio).

## Problemi noti (bug check del 2026-10-05, da sistemare)

Tutti riprodotti con file di prova, tranne dove indicato.

- **Sincronia audio/video attorno alle sigle**: video e audio vengono concatenati con due filtri separati, quindi se nell'episodio l'audio è più lungo/corto del video (o parte in ritardo) la differenza si accumula e l'audio della sigla finale esce sfasato (provato: audio più lungo di 2 s → sfasamento di 2 s). Con file ben fatti la differenza è di decine di ms. Correzione provata a mano: un solo `concat` con `v=1:a=N`.
- **Coda modificata durante la codifica**: caricare/trascinare una nuova cartella o dei file mentre un batch è in corso, e poi premere Stop, lascia il programma bloccato per sempre (`running` resta vero). Mancano blocchi su drop/sfoglia durante l'esecuzione e l'analisi.
- **AV1 software (libsvtav1) non parte mai**: il preset passato è `fast/medium/slow` ma libsvtav1 vuole un numero (-2..13). I 2 passaggi funzionano con preset numerico.
- **"Contenitore: come originale"** fallisce per episodi `.webm`, `.mpg`, `.mpeg` (muxer incompatibile con H.264/AAC); H.265 fallisce anche in `.wmv`. Il messaggio d'errore è criptico.
- **Sottotitoli in contenitori che non li supportano** (`.avi`, `.flv`, `.wmv`): l'errore arriva solo dopo l'intera codifica (tempo sprecato). Con `.mov`, copertina + sottotitoli fallisce nel secondo passaggio (cerca un flusso `0:v:1` che non c'è). I sottotitoli a immagine (PGS/VobSub) verso MP4 non sono stati testati ma falliranno per lo stesso motivo.
- **Video ruotati** (metadato di rotazione, tipico dei video da telefono): le dimensioni di riferimento sono quelle "grezze", quindi l'episodio esce come striscia stretta con grandi bande nere. Correzione provata: scambiare larghezza/altezza se la rotazione è 90/270.
- **Video interlacciati** (vecchi DVD/TV): in uscita il flag di interlacciamento si perde (`progressive`), quindi il lettore non deinterlaccia più e si vede l'effetto "pettine" nei movimenti.
- **Video a 10 bit**: l'uscita è sempre a 8 bit (rischio banding sui gradienti, comune nei cartoni a 10 bit); il materiale HDR perde la sua gamma.
- **Stesso nome, estensione diversa** (`Puntata 1.mp4` e `Puntata 1.avi`) con un contenitore fisso: i due lavori usano lo stesso file di output e lo stesso file temporaneo, uno dei due fallisce con errore criptico.
- **Chiudere la finestra durante la codifica** non annulla pulitamente: ffmpeg si interrompe da solo ma lascia nella cartella di output il file nascosto `.part` (e i file del 2 passaggi).
- Non verificati (solo lettura del codice o impossibile qui): `avg_frame_rate` "0/0" fa ignorare `r_frame_rate` (si ripiega su 25 fps); con la GPU un numero di elaborazioni parallele superiore al limite delle sessioni NVENC può far fallire alcuni file; più file "sigla iniziale" nella stessa cartella: vince l'ultimo, gli altri spariscono dalla coda senza avviso; cartella di output uguale alla sorgente: una nuova esecuzione ricodifica anche i file prodotti.

## Struttura del progetto

```
main.py             GUI (PySide6)
merger.py           Logica di unione video: probing, comando ffmpeg, verifica integrità
utils.py            Rilevamento ffmpeg/ffprobe, encoder hardware, utility varie
assets/             Icone usate dall'interfaccia
requirements.txt    Dipendenze Python
ClipMerger.spec     Configurazione build PyInstaller
build.bat           Script di compilazione
```
