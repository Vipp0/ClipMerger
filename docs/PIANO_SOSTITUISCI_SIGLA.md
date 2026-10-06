# ClipMerger — scheda "Sostituisci sigla" (piano adattato al progetto attuale)

## Contesto

ClipMerger (PySide6, `main.py` + `merger.py` + `utils.py`) oggi unisce sigla iniziale + episodio + sigla finale. Fabrizio vuole una **seconda scheda** che lavora su sigle già presenti negli episodi:

- **Modalità A, solo audio**: il video della sigla è giusto, ma l'audio va sostituito (es. canzone italiana al posto di quella inglese/giapponese). Video copiato senza ricodifica.
- **Modalità B, audio + video**: la sigla straniera è già attaccata all'episodio; il programma la individua, la taglia e ci mette quella italiana (audio e video), con ricodifica ad alta qualità.

Il piano originale incollato faceva riferimento a un altro progetto (CustomTkinter, "percorso FFmpeg" condiviso, changelog "✅ Fixato —", Spot Cutter). Qui è adattato a ClipMerger com'è davvero.

## Decisioni già prese con Fabrizio

1. **Come si ottiene il campione della sigla da togliere**: o un file separato fornito dall'utente, **oppure il programma "impara" la sigla scansionando la cartella degli episodi** (la sigla è uguale in ogni episodio). Niente strumento di ritaglio manuale.
2. **Qualità in modalità B**: prima versione = ricodifica dell'intero episodio ad alta qualità (stesso codec, bitrate vicino all'originale). Il "taglio intelligente" (copiare tutto tranne i pezzi vicini ai tagli) resta per una fase successiva.
3. **Impostazioni di codifica**: la scheda 2 ha un suo riquadro essenziale (codec, qualità, GPU/CPU, preset); la scheda 1 resta identica a oggi.
4. Changelog nel nostro formato ("### Novità" / "### Correzioni"), non quello del piano originale.

## Regole di lavoro

- Scheda 1 invariata nel comportamento. Su `main.py` solo gli agganci minimi (vedi sotto).
- Logica nuova in file nuovi. Le funzioni esistenti si estendono solo con parametri opzionali che, di default, producono esattamente lo stesso risultato (verificato confrontando i comandi ffmpeg generati prima/dopo).
- Si lavora per fasi e ci si ferma a fine fase per il collaudo; build di prova (zip) a fine fasi 2, 3, 4, 5; release pubblica solo dopo il via libera.
- Verifiche reali (ffmpeg veri, file di prova con "verità nota"), come fatto finora.

## File

| File | Stato | Ruolo |
| --- | --- | --- |
| `trova_sigla.py` | nuovo | Motore di riconoscimento, nessuna GUI, usabile da riga di comando |
| `sostituisci.py` | nuovo | Costruzione/esecuzione dei comandi ffmpeg per modalità A e B |
| `tab_sostituisci.py` | nuovo | Interfaccia della scheda 2 (widget Qt), thread di analisi/elaborazione |
| `main.py` | agganci | Contenitore a schede (vedi sotto) |
| `merger.py` | estensione opzionale | `_build_filter_complex` accetta (opzionale) un intervallo di taglio per segmento |
| `requirements.txt`, `ClipMerger.spec` | piccoli | Aggiunta di **numpy** (niente scipy: la FFT di numpy basta, scipy gonfia l'eseguibile) |
| `docs/PIANO_SOSTITUISCI_SIGLA.md`, `README.md` | nuovo/aggiornato | Questo piano salvato nel repo + voce in Roadmap |

### Aggancio in `main.py` (minimo)
In `MainWindow._build_ui` (riga ~435) il contenuto attuale è costruito dentro `central` e impostato con `self.setCentralWidget(central)`. Modifica: togliere quella `setCentralWidget(central)` iniziale e, **a fine `_build_ui`**, creare un `QTabWidget` con `central` come prima scheda ("Unisci sigle") e `SostituisciTab(self)` come seconda, poi `self.setCentralWidget(tabs)`. (Chiamare `setCentralWidget` due volte distruggerebbe il primo widget.) La scheda 2 legge da `MainWindow`: `ffmpeg_status`, `hw_encoders`/`hw_reasons` (rilevamento QSV/NVENC già fatto all'avvio), e usa un **proprio** `QThreadPool` per non interferire con la scheda 1.

## Motore di riconoscimento (`trova_sigla.py`)

Solo numpy + ffmpeg (estrazione audio mono ~8 kHz tramite pipe, come fa già `utils._run_quiet`/`merger`).

- **Caratteristiche**: STFT a finestre brevi, energie logaritmiche per bande, normalizzate per frame → regge differenze di volume ed equalizzazione.
- **`cerca(episodio, campione, zona)`**: spezza il campione in blocchi (~5 s), li cerca nella zona (apertura = primi N min, predefinito 6; chiusura = ultimi N; tutto) con cross-correlazione normalizzata via FFT, tiene solo i blocchi in posizioni coerenti (inizio = primo, fine = ultimo → regge sigle accorciate), rifinisce i bordi. Esito: inizio/fine al centesimo, affidabilità 0–100, offset interno nel campione, stato (trovata / non trovata / più volte).
- **`impara(cartella, zona)`** (nuovo rispetto al piano originale): per ogni episodio estrae la zona; sceglie un episodio-seme; per i suoi blocchi conta in quanti altri episodi compare un match forte; la sigla è la **sequenza più lunga di blocchi con consenso ≥ max(2, 60% degli episodi)**; da lì ricava inizio/fine in ogni episodio e salva il campione appreso (WAV + spezzone video del seme per l'anteprima). Richiede almeno 3 episodi. Rischio noto: jingle ricorrenti diversi dalla sigla (riassunti, stacchetti) → l'utente vede il risultato nella tabella e nell'anteprima prima di elaborare.
- **CLI**: `python trova_sigla.py cerca episodio.mkv sigla.wav` e `python trova_sigla.py impara cartella\` → stampa inizio, fine, affidabilità.

## Modalità A: solo audio (`sostituisci.py`)

Video e tutto il resto copiati (`-map 0 -c copy`); le tracce audio scelte vengono rifatte per intero (non si può incollare un pezzo dentro un AAC/AC3 senza ricodificare): prima parte + sigla nuova + dopo, con dissolvenze di ~30 ms ai bordi, stessi codec/canali/bitrate dell'originale (riuso `merger._plan_audio` in modalità "Come originale"). Allineamento della sigla nuova: **sulla musica** (correlazione col campione, predefinito se è lo stesso brano), **adatta velocità** (`atempo`, ±5%), **sfuma alla fine** se più lunga, **silenzio / audio originale** se più corta. Volume regolabile (e opzione per uniformarlo all'episodio). Scelta delle tracce su cui agire.

## Modalità B: audio + video (`sostituisci.py`)

Episodio = parte prima [0..t0] + sigla nuova adattata + parte dopo [t1..fine], una sola ricodifica. Riuso del montaggio già esistente: `merger._build_filter_complex` (scala/pad con SAR, fps, interlacciato, piani audio, **concat unico audio+video sincronizzato**) esteso con taglio opzionale `trim/atrim + setpts` per segmento. Encoder: come scheda 1 (QSV/NVENC se rilevati, altrimenti software), qualità alta di default (`_rate_control_args`, `_encoder_preset`, `pick_container`). Adattamento 4:3 → 16:9 con scelta bande/ritaglio/allargamento.
Tracce legate al tempo:
- **Sottotitoli di testo** (srt/ass): tolti quelli dentro la vecchia sigla, quelli dopo spostati della differenza di durata (elaborati in Python; ass mantiene gli stili). Sottotitoli a immagine: saltati con nota (come già in `merger.subtitles_for_container`).
- **Capitoli**: ricalcolati con la stessa logica di `merger.write_chapters_file` (offset), quelli dentro la vecchia sigla accorpati.
- Più tracce audio nell'episodio e una sola nella sigla nuova → la sigla nuova è messa su tutte.
- Frame rate variabile: `fps=` già forza il costante.

## Interfaccia della scheda 2

Due tempi: **Analizza** (trova/impara, mostra la tabella) poi **Elabora** (scrive). Niente viene scritto prima che l'utente veda i risultati.
- Alto: modalità A/B, apertura/chiusura, cartella episodi, sigla da togliere (file **oppure** "Impara dalla cartella"), sigla sostitutiva, opzioni della modalità (allineamento, proporzioni, volume, tracce), soglia di affidabilità (70), riquadro codifica essenziale.
- Centro: tabella (Episodio · Inizio/Fine modificabili · Affidabilità · Stato · Includi). Stati: Trovata, Da verificare, Non trovata, Già elaborato, Fatto, Errore.
- Pulsanti riga: **Anteprima giunzioni** (due clip di ~10 s intorno a ingresso e uscita, aperte nel lettore di Windows), **Apri episodio al punto**.
- Basso: Elabora/Stop (stesso schema Avvia→Stop di scheda 1), barra, log. Output in sottocartella, salto dei file già fatti, annullamento pulito e blocco della coda durante l'elaborazione (lezioni già imparate nella scheda 1: `_queue_locked`, `closeEvent`, thread di analisi con contatore come `PreflightWorker`).
- Profili per serie (sigla appresa, sostitutiva, opzioni) salvati in JSON: si sovrappone ai "preset salvabili" della Roadmap → un solo meccanismo per entrambi (fase 5).

## Casi limite
Non trovata → saltata e segnalata; più match → "Da verificare", proposta la prima; sigla accorciata → la ricerca a blocchi la gestisce e la tabella mostra la parte presente; dialoghi sopra la sigla → affidabilità più bassa + avviso; errore ffmpeg su un episodio → "Errore", si prosegue; file già elaborato → saltato; meno di 3 episodi in "Impara" → messaggio chiaro.

## Fasi e collaudo (ci si ferma a fine di ognuna)

0. **Documentazione**: salvare questo piano in `docs/PIANO_SOSTITUISCI_SIGLA.md` + voce in Roadmap del README.
1. **Motore** (`trova_sigla.py`, solo CLI). Collaudo automatico con **verità nota**: episodi sintetici (voce/musica + la sigla inserita a un tempo noto, con volume/equalizzazione diversi, sigla accorciata, sigla assente, sigla due volte) → errore ≤ 0,05 s; poi su 5–10 episodi veri di Fabrizio, entro mezzo secondo dal controllo a mano. Poi prova di "impara" su una cartella vera.
2. **Schede + scheletro scheda 2** (aggancio in `main.py`, impostazioni, Analizza, tabella). Collaudo: scheda 1 identica (confronto dei comandi ffmpeg generati prima/dopo + gli script GUI già usati); l'analisi riempie la tabella. Build di prova.
3. **Modalità A** + anteprima giunzioni. Collaudo: video identico bit a bit (stream copy), nessuno scatto ai bordi, altre tracce intatte.
4. **Modalità B** + sottotitoli/capitoli. Collaudo: sigla 4:3 dentro episodio 16:9, sottotitoli ancora a tempo, durata e sincronia corrette (misura A/V come fatto in questa sessione), qualità confrontata con l'originale.
5. **Rifinitura**: profili, correzione manuale dei tempi, eseguibile PyInstaller con numpy (controllo dimensione, prova senza Python), README/changelog.

## Verifica end-to-end
Per ogni fase: script di prova in scratchpad con file reali (ffmpeg/ffprobe veri), misure oggettive (tempi trovati vs verità nota, `idet`/durate/sfasamento audio-video, confronto dei comandi della scheda 1), prova della GUI con la finestra vera (offscreen) e avvio dell'eseguibile compilato.

## Fuori ambito per ora
Taglio intelligente senza ricodifica, riconoscimento dal video, altri elementi ricorrenti (stacchi, cartelli), riuso del motore in altri programmi.
