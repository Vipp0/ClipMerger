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
- **Sincronia audio/video** garantita attorno alle sigle anche con file "sporchi" (audio più lungo o in ritardo rispetto al video): video e audio si concatenano insieme.
- **Video ruotati** (metadato di rotazione, tipico dei video da telefono) gestiti correttamente.
- **Deinterlaccia automatica** dei video segnalati come interlacciati (vecchi DVD/TV), disattivabile; **10 bit mantenuti** per H.265/AV1 software se l'episodio è a 10 bit.
- **Contenitore di ripiego**: con "come originale", se l'estensione dell'episodio non può contenere il codec scelto (es. `.webm`, `.mpg`) il file viene salvato come `.mkv`; nomi di output duplicati vengono numerati. I sottotitoli non supportati dal formato di uscita vengono saltati (con nota nello stato) invece di far fallire il file.
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

## Limiti noti

- **Materiale HDR**: viene ricodificato senza conversione della gamma di colori (il 10 bit si conserva, ma i metadati HDR no).
- **Interlacciamento non dichiarato**: viene riconosciuto solo se il file lo segnala (`field_order`); un video interlacciato senza flag non viene deinterlacciato.
- **Con la scheda video** (non verificato qui, nessuna NVIDIA/AMD disponibile): un numero di elaborazioni parallele superiore al limite delle sessioni NVENC può far fallire alcuni file, e su AMD il preset scelto non ha effetto (AMF non usa `-preset`).
- **Copertine in MP4** conservate come immagine incorporata; i font allegati dei MKV non possono stare in un MP4.
- **Sottotitoli a immagine** (PGS/VobSub) si copiano solo in MKV; verso altri formati vengono saltati e lo stato della riga lo segnala.

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
