# ClipMerger

Applicazione desktop per Windows che unisce automaticamente una **sigla iniziale** e una **sigla finale** a un batch di episodi (cartoni animati, programmi TV, ecc.), producendo per ciascun video un nuovo file: `sigla iniziale + episodio + sigla finale`.

## Funzionalità

- **Coda batch** con drag & drop di una cartella intera (o di singoli file video) e barra di avanzamento per ogni video.
- **Riconoscimento automatico delle sigle**: se nella cartella trascinata un file contiene "sigla iniziale" o "sigla finale" nel nome, viene assegnato da solo ai campi corrispondenti invece di finire in coda come episodio.
- **Ricodifica robusta**: episodio, sigla iniziale e sigla finale possono avere risoluzione, framerate o codec diversi tra loro — l'episodio fa sempre da riferimento e le sigle vengono adattate.
- **Codec**: H.264, H.265 (HEVC), AV1, con rilevamento automatico e uso opzionale dell'accelerazione hardware (NVIDIA NVENC, Intel QuickSync, AMD AMF) se disponibile.
- **Controllo qualità/peso**: qualità costante (CRF), bitrate costante (CBR), oppure bitrate allineato all'originale per minimizzare la perdita percepita.
- **Ottimizzazione per cartoni animati** (`tune animation`, solo codifica software H.264/H.265).
- Gestione di **tracce audio multiple** e **sottotitoli** dell'episodio, mantenuti sincronizzati nel file finale.
- **Verifica di integrità** automatica dell'output (controllo durata) al termine di ogni file.
- **Codifica a 2 passaggi** (bitrate medio) per software H.264/H.265/AV1: stesso bitrate target, qualità distribuita meglio tra le scene.
- **Tempo rimanente stimato**, per singolo file e per l'intero batch.
- **Riepilogo di controllo** (durate, risoluzioni, avvisi) prima di avviare il batch, con possibilità di annullare.
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
