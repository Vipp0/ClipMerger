"""Scheda "Dividi puntate": taglia i file che contengono tante puntate (o sketch) in un file per puntata,
col nome «1x012 Titolo». Due tempi: "Analizza" trova i tagli e legge i titoli e li mostra in tabella
(si può correggere tutto), "Avvia" scrive i file nuovi: niente viene scritto prima che l'utente veda la tabella."""
from __future__ import annotations

import re
import tempfile
import threading
from pathlib import Path

from PySide6.QtCore import QObject, QRect, QRunnable, QSize, Qt, QThread, QThreadPool, Signal
from PySide6.QtGui import QBrush, QColor, QIcon, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView, QComboBox, QDialog, QFileDialog, QGridLayout, QGroupBox, QHBoxLayout, QHeaderView, QLabel,
    QLineEdit, QMessageBox, QProgressBar, QPushButton, QSpinBox, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

import applog
import dividi
import merger
import titoli
from tab_sostituisci import FolderDropEdit, fmt_time, parse_time
from utils import list_video_files

PRESETS = [("Veloce", "fast"), ("Bilanciato", "medium"), ("Qualità", "slow")]
CODECS = [("Come l'originale", "original"), ("H.265 (file più piccoli)", "h265")]
COL_CHECK, COL_FILE, COL_START, COL_END, COL_DUR, COL_NUM, COL_TITLE, COL_STATUS, COL_SHOT, COL_PROGRESS = range(10)
COL_EDITABLE = (COL_START, COL_END, COL_NUM, COL_TITLE)
WORK_DIR = Path(tempfile.gettempdir()) / "ClipMerger_dividi"
_AMBER = QBrush(QColor(255, 193, 7, 70))


def short_name(path: Path) -> str:
    m = re.search(r"[Ss]\d{1,2}[Ee]\d{1,3}", path.name)
    return m.group(0).upper() if m else path.stem[:24]


class AnalyzeWorker(QThread):
    progress = Signal(float, str)
    done = Signal(list, list)
    failed = Signal(str)

    def __init__(self, ffmpeg, ffprobe, files, profile, series_id, season, key):
        super().__init__()
        self.args = (ffmpeg, ffprobe, files, profile, series_id, season, key)

    def run(self):
        ffmpeg, ffprobe, files, profile, series_id, season, key = self.args
        try:
            catalogue = titoli.cached_season(series_id, season)
            if catalogue is None:
                self.progress.emit(0.0, "Scarico la lista dei titoli da TMDB...")
                catalogue = titoli.download_season(series_id, season, key)
            result = dividi.analizza(
                files, profile, catalogue, ffmpeg, ffprobe, WORK_DIR,
                progress=lambda f, text: self.progress.emit(f, text), cancelled=self.isInterruptionRequested)
        except dividi.Annullato:
            return
        except (titoli.TitoliError, dividi.DividiError) as exc:
            self.failed.emit(str(exc))
            return
        except Exception as exc:  # never let the thread die silently
            applog.log_exception("Dividi puntate (analisi)", exc)
            self.failed.emit(applog.describe_exception(exc))
            return
        self.done.emit(result, catalogue)


class DownloadWorker(QThread):
    done = Signal(list)
    failed = Signal(str)

    def __init__(self, series_id: int, season: int, key: str):
        super().__init__()
        self.args = (series_id, season, key)

    def run(self):
        try:
            self.done.emit(titoli.download_season(*self.args))
        except titoli.TitoliError as exc:
            self.failed.emit(str(exc))
        except Exception as exc:
            applog.log_exception("Dividi puntate (lista TMDB)", exc)
            self.failed.emit(applog.describe_exception(exc))


class ProcessSignals(QObject):
    progress = Signal(int, float)
    status = Signal(int, str)
    finished = Signal(int, object)


class ProcessTask(QRunnable):
    def __init__(self, row: int, ffmpeg: str, ffprobe: str, src: Path, start: float, end: float, output: Path,
                 crf: int, preset: str, codec: str, title: str, signals: ProcessSignals, cancel_event: threading.Event):
        super().__init__()
        self.row = row
        self.args = (ffmpeg, ffprobe, src, start, end, output, crf, preset, codec, title)
        self.signals, self.cancel_event = signals, cancel_event
        self.setAutoDelete(True)

    def run(self):
        self.signals.status.emit(self.row, "In elaborazione")
        try:
            result = dividi.cut_piece(
                *self.args, progress_cb=lambda pct: self.signals.progress.emit(self.row, pct),
                cancel_event=self.cancel_event)
        except Exception as exc:
            applog.log_exception("Dividi puntate", exc)
            result = merger.MergeResult(output_path=Path(), success=False, error=applog.describe_exception(exc))
        self.signals.finished.emit(self.row, result)


class DividiTab(QWidget):
    queue_finished = Signal()  # a run ended by itself (not stopped by the user)

    def __init__(self, main_window):
        super().__init__()
        self.win = main_window
        self.files: list[Path] = []
        self.entries: list[tuple[Path, dividi.Piece | None, str]] = []  # (file, piece or None, error text)
        self.catalogue: list[titoli.Episodio] = []
        self.season = 1
        self.state = "idle"  # "idle" | "analyzing" | "processing"
        self.analyze_worker: AnalyzeWorker | None = None
        self.download_worker: DownloadWorker | None = None
        self.retired_workers: list[QThread] = []
        self.pool = QThreadPool()
        self.signals = ProcessSignals()
        self.cancel_event = threading.Event()
        self.started: set[int] = set()
        self.job_rows: list[int] = []
        self.row_pct: dict[int, float] = {}
        self.finished_count = self.total_count = 0
        self.ok_count = self.skip_count = self.err_count = 0
        self._filling = False
        self._build_ui()
        self._load_settings()
        self.signals.progress.connect(self._on_progress)
        self.signals.status.connect(self._on_status)
        self.signals.finished.connect(self._on_finished)

    # ------------------------------------------------------------------ UI
    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setSpacing(10)

        top = QGridLayout()
        top.setSpacing(8)
        top.addWidget(QLabel("File o cartella:"), 0, 0)
        self.folder_edit = FolderDropEdit("Trascina qui la cartella con i file da dividere, oppure sfoglia...")
        self.folder_edit.folder_dropped.connect(self._load_folder)
        top.addWidget(self.folder_edit, 0, 1)
        browse_folder = QPushButton("Cartella...")
        browse_folder.setObjectName("secondary")
        browse_folder.clicked.connect(self._browse_folder)
        top.addWidget(browse_folder, 0, 2)
        browse_files = QPushButton("File...")
        browse_files.setObjectName("secondary")
        browse_files.clicked.connect(self._browse_files)
        top.addWidget(browse_files, 0, 3)
        top.addWidget(QLabel("Cartella output:"), 1, 0)
        self.output_edit = QLineEdit()
        self.output_edit.setPlaceholderText("<cartella dei file>/puntate divise")
        top.addWidget(self.output_edit, 1, 1)
        browse_out = QPushButton("Sfoglia...")
        browse_out.setObjectName("secondary")
        browse_out.clicked.connect(self._browse_output)
        top.addWidget(browse_out, 1, 2, 1, 2)
        top.setColumnStretch(1, 1)
        top_box = QGroupBox("File da dividere")
        top_box.setLayout(top)
        root.addWidget(top_box)

        prof = QGridLayout()
        prof.setSpacing(8)
        prof.addWidget(QLabel("Profilo:"), 0, 0)
        self.profile_combo = QComboBox()
        for key, profile in dividi.PROFILES.items():
            self.profile_combo.addItem(profile.name, key)
        prof.addWidget(self.profile_combo, 0, 1, 1, 5)
        info = QLabel(
            "Ogni puntata comincia con lo stesso filmato (qui il bicchiere di Camera Café) e il suo titolo compare "
            "sullo schermo. Si taglia lì, si scartano l'apertura iniziale e i titoli di coda, e il primo pezzo "
            "tiene anche il breve inizio che precede il primo titolo. Nessuna sigla viene aggiunta.")
        info.setWordWrap(True)
        prof.addWidget(info, 1, 0, 1, 6)
        prof.addWidget(QLabel("Serie TMDB (numero):"), 2, 0)
        self.series_spin = QSpinBox()
        self.series_spin.setRange(1, 99999999)
        self.series_spin.setToolTip("Il numero che si vede nell'indirizzo della pagina su themoviedb.org (Camera Café: 63809).")
        prof.addWidget(self.series_spin, 2, 1)
        prof.addWidget(QLabel("Stagione:"), 2, 2)
        self.season_spin = QSpinBox()
        self.season_spin.setRange(1, 99)
        self.season_spin.valueChanged.connect(self._refresh_list_status)
        self.series_spin.valueChanged.connect(self._refresh_list_status)
        prof.addWidget(self.season_spin, 2, 3)
        prof.addWidget(QLabel("Chiave TMDB:"), 2, 4)
        self.key_edit = QLineEdit()
        self.key_edit.setEchoMode(QLineEdit.Password)
        self.key_edit.setPlaceholderText("serve solo per scaricare una lista nuova")
        self.key_edit.setToolTip(
            "La tua chiave personale di TMDB (themoviedb.org → Impostazioni → API). Resta su questo PC, "
            "nelle impostazioni del programma. Una volta scaricata la lista di una stagione non serve più.")
        self.key_edit.editingFinished.connect(self._save_settings)
        prof.addWidget(self.key_edit, 2, 5)
        self.list_btn = QPushButton("Scarica la lista")
        self.list_btn.setObjectName("secondary")
        self.list_btn.clicked.connect(self._download_list)
        prof.addWidget(self.list_btn, 3, 3)
        self.list_label = QLabel("")
        prof.addWidget(self.list_label, 3, 4, 1, 2)
        prof.setColumnStretch(5, 1)
        prof_box = QGroupBox("Profilo e titoli (TMDB)")
        prof_box.setLayout(prof)
        root.addWidget(prof_box)

        enc = QGridLayout()
        enc.setSpacing(8)
        enc.addWidget(QLabel("Qualità:"), 0, 0)
        self.quality_combo = QComboBox()
        for label, level in dividi.QUALITY_LEVELS:
            self.quality_combo.addItem(f"{label}  (CRF {level})", level)
        self.quality_combo.setCurrentIndex(1)
        enc.addWidget(self.quality_combo, 0, 1)
        enc.addWidget(QLabel("Preset:"), 0, 2)
        self.preset_combo = QComboBox()
        for label, value in PRESETS:
            self.preset_combo.addItem(label, value)
        self.preset_combo.setCurrentIndex(1)
        enc.addWidget(self.preset_combo, 0, 3)
        enc.addWidget(QLabel("Codec:"), 0, 4)
        self.codec_combo = QComboBox()
        for label, value in CODECS:
            self.codec_combo.addItem(label, value)
        enc.addWidget(self.codec_combo, 0, 5)
        enc.addWidget(QLabel("Elaborazioni parallele:"), 0, 6)
        self.parallel_spin = QSpinBox()
        self.parallel_spin.setRange(1, 4)
        self.parallel_spin.setValue(2)
        enc.addWidget(self.parallel_spin, 0, 7)
        note = QLabel(
            "Ogni pezzo viene ricodificato una sola volta (il taglio è preciso al fotogramma), con codifica software, "
            "nello stesso formato del file. Il bitrate non supera mai quello dell'originale; l'audio resta com'è "
            "(stesso codec e tracce). Con «Bilanciata» i pezzi escono più piccoli dell'originale.")
        note.setWordWrap(True)
        enc.addWidget(note, 1, 0, 1, 8)
        enc.setColumnStretch(1, 1)
        enc_box = QGroupBox("Codifica")
        enc_box.setLayout(enc)
        root.addWidget(enc_box)

        buttons = QHBoxLayout()
        self.analyze_btn = QPushButton("Analizza")
        self.analyze_btn.clicked.connect(self._on_analyze_clicked)
        buttons.addWidget(self.analyze_btn)
        buttons.addStretch(1)
        self.start_btn = QPushButton("Avvia")
        self.start_btn.clicked.connect(self._on_start_clicked)
        buttons.addWidget(self.start_btn)
        root.addLayout(buttons)

        self.table = QTableWidget(0, 10)
        self.table.setHorizontalHeaderLabels(
            ["", "File", "Inizio", "Fine", "Durata", "N°", "Titolo", "Stato", "Titolo sullo schermo", "Avanzamento"])
        header = self.table.horizontalHeader()
        for col in (COL_CHECK, COL_FILE, COL_START, COL_END, COL_DUR, COL_NUM, COL_STATUS):
            header.setSectionResizeMode(col, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(COL_TITLE, QHeaderView.Stretch)
        header.setSectionResizeMode(COL_SHOT, QHeaderView.Fixed)
        self.table.setColumnWidth(COL_SHOT, 400)
        header.setSectionResizeMode(COL_PROGRESS, QHeaderView.Fixed)
        self.table.setColumnWidth(COL_PROGRESS, 120)
        self.table.setIconSize(QSize(380, 34))
        self.table.verticalHeader().setDefaultSectionSize(42)
        self.table.setEditTriggers(QAbstractItemView.DoubleClicked | QAbstractItemView.EditKeyPressed)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.cellDoubleClicked.connect(self._on_double_click)
        self.table.itemChanged.connect(self._on_item_changed)
        self.table.setToolTip(
            "Doppio clic su Inizio, Fine, N° o Titolo per correggere a mano (i tempi come minuti:secondi, es. 4:51.3). "
            "Se scrivi un titolo che è nella lista di TMDB, il numero si aggiorna da solo. "
            "Doppio clic sull'immagine per vederla ingrandita.")
        self.table.setMinimumHeight(200)
        root.addWidget(self.table, stretch=1)

        bottom = QHBoxLayout()
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        bottom.addWidget(self.progress, stretch=1)
        self.info_label = QLabel("")
        bottom.addWidget(self.info_label)
        root.addLayout(bottom)
        self._refresh_buttons()

    # ------------------------------------------------------------------ settings, helpers
    def _load_settings(self):
        qs = self.win.qsettings
        self.series_spin.setValue(int(qs.value("dividi/serie", dividi.PROFILES["camera_cafe"].series_id or 1)))
        self.season_spin.setValue(int(qs.value("dividi/stagione", 1)))
        self.key_edit.setText(str(qs.value("dividi/chiave", "")))
        self._refresh_list_status()

    def _save_settings(self):
        qs = self.win.qsettings
        qs.setValue("dividi/serie", self.series_spin.value())
        qs.setValue("dividi/stagione", self.season_spin.value())
        qs.setValue("dividi/chiave", self.key_edit.text().strip())

    def _profile(self) -> dividi.Profile:
        return dividi.PROFILES[self.profile_combo.currentData()]

    def _key(self) -> str:
        return self.key_edit.text().strip() or titoli.key_from_file()

    def _refresh_list_status(self):
        cached = titoli.cached_season(self.series_spin.value(), self.season_spin.value())
        self.list_label.setText(f"Lista in memoria: {len(cached)} titoli" if cached else "Nessuna lista: serve la chiave")

    def _tools(self) -> tuple[str, str] | None:
        status = self.win.ffmpeg_status
        if not status.ffmpeg_path or not status.ffprobe_path:
            QMessageBox.warning(self, "ffmpeg mancante", "ffmpeg/ffprobe non sono stati trovati.")
            return None
        return status.ffmpeg_path, status.ffprobe_path

    def is_busy(self) -> bool:
        return self.state != "idle"

    def shutdown(self):
        """Stops whatever is running (called when the window closes)."""
        self._save_settings()
        self.cancel_event.set()
        self.pool.clear()
        self.pool.waitForDone(20000)
        for worker in [self.analyze_worker, self.download_worker, *self.retired_workers]:
            if worker is not None and worker.isRunning():
                worker.requestInterruption()
                worker.wait(5000)

    def _refresh_buttons(self):
        idle = self.state == "idle"
        self.analyze_btn.setText("Annulla" if self.state == "analyzing" else "Analizza")
        self.analyze_btn.setEnabled(idle or self.state == "analyzing")
        self.start_btn.setText("Stop" if self.state == "processing" else "Avvia")
        self.start_btn.setEnabled(self.state == "processing" or (idle and any(p is not None for _, p, _ in self.entries)))
        for w in (self.folder_edit, self.output_edit, self.profile_combo, self.series_spin, self.season_spin, self.key_edit,
                  self.list_btn, self.quality_combo, self.preset_combo, self.codec_combo, self.parallel_spin):
            w.setEnabled(idle)
        self.table.setEditTriggers(
            (QAbstractItemView.DoubleClicked | QAbstractItemView.EditKeyPressed) if idle else QAbstractItemView.NoEditTriggers)
        for name in ("analyze_btn", "start_btn"):
            btn = getattr(self, name)
            btn.setObjectName("danger" if btn.text() in ("Annulla", "Stop") else "")
            btn.style().unpolish(btn)
            btn.style().polish(btn)

    def _set_state(self, state: str):
        self.state = state
        self._refresh_buttons()

    # ------------------------------------------------------------------ files
    def _browse_folder(self):
        if self.is_busy():
            return
        folder = QFileDialog.getExistingDirectory(self, "Seleziona la cartella con i file da dividere")
        if folder:
            self._load_folder(folder)

    def _browse_files(self):
        if self.is_busy():
            return
        paths, _ = QFileDialog.getOpenFileNames(self, "Seleziona i file da dividere", "", "Video (*.mkv *.mp4 *.avi *.mov *.ts *.mpg *.mpeg)")
        if paths:
            self._set_files([Path(p) for p in paths], str(Path(paths[0]).parent))

    def _browse_output(self):
        folder = QFileDialog.getExistingDirectory(self, "Seleziona la cartella di output")
        if folder:
            self.output_edit.setText(folder)

    def _load_folder(self, folder: str):
        if self.is_busy():
            return
        files = list_video_files(Path(folder))
        if not files:
            QMessageBox.warning(self, "Nessun video", "Nessun video trovato in questa cartella.")
            return
        self._set_files(files, folder)

    def _set_files(self, files: list[Path], folder: str):
        self.files = files
        self.folder_edit.setText(folder if len(files) != 1 else str(files[0]))
        self.output_edit.setText(str(Path(folder) / "puntate divise"))
        season = titoli.season_from_names([f.name for f in files])
        if season is not None:
            self.season_spin.setValue(season)
        self.entries = []
        self.table.setRowCount(0)
        self._refresh_buttons()
        self.info_label.setText(f"{len(files)} file. Premi Analizza per trovare le puntate.")

    # ------------------------------------------------------------------ TMDB list
    def _download_list(self):
        key = self._key()
        if not key:
            QMessageBox.information(self, "Chiave mancante", "Scrivi nel campo la tua chiave di TMDB, poi premi di nuovo.")
            return
        self._save_settings()
        self.list_btn.setEnabled(False)
        self.list_label.setText("Scarico...")
        worker = DownloadWorker(self.series_spin.value(), self.season_spin.value(), key)
        worker.done.connect(lambda eps: self._after_download(f"Lista scaricata: {len(eps)} titoli"))
        worker.failed.connect(lambda msg: (self._after_download(""), QMessageBox.warning(self, "Lista non scaricata", msg)))
        self.download_worker = worker
        worker.start()

    def _after_download(self, text: str):
        self.list_btn.setEnabled(self.state == "idle")
        self._refresh_list_status()
        if text:
            self.list_label.setText(text)

    # ------------------------------------------------------------------ analysis
    def _on_analyze_clicked(self):
        if self.state == "analyzing":
            self._cancel_analysis()
            return
        if not self.files:
            QMessageBox.warning(self, "Dati mancanti", "Seleziona prima una cartella o dei file.")
            return
        series, season = self.series_spin.value(), self.season_spin.value()
        if titoli.cached_season(series, season) is None and not self._key():
            QMessageBox.warning(
                self, "Lista dei titoli mancante",
                "Per questa serie e stagione non c'è ancora la lista dei titoli: scrivi la tua chiave di TMDB nel campo "
                "apposito e riprova (la lista si scarica una volta sola).")
            return
        tools = self._tools()
        if tools is None:
            return
        self._save_settings()
        self.season = season
        self.entries = []
        self.table.setRowCount(0)
        self.progress.setValue(0)
        self.info_label.setText("Analisi in corso: ci vuole circa mezzo minuto per file...")
        worker = AnalyzeWorker(tools[0], tools[1], list(self.files), self._profile(), series, season, self._key())
        worker.progress.connect(self._on_analysis_progress)
        worker.done.connect(self._on_analysis_done)
        worker.failed.connect(self._on_analysis_failed)
        self.analyze_worker = worker
        worker.start()
        self._set_state("analyzing")

    def _cancel_analysis(self):
        worker = self.analyze_worker
        if worker is not None:
            for sig, slot in ((worker.progress, self._on_analysis_progress), (worker.done, self._on_analysis_done),
                              (worker.failed, self._on_analysis_failed)):
                sig.disconnect(slot)
            worker.requestInterruption()
            self.retired_workers = [w for w in self.retired_workers if w.isRunning()] + [worker]
            self.analyze_worker = None
        self.progress.setValue(0)
        self.info_label.setText("Analisi annullata.")
        self._set_state("idle")

    def _on_analysis_progress(self, fraction: float, text: str):
        self.progress.setValue(int(fraction * 100))
        self.info_label.setText(text)

    def _on_analysis_failed(self, message: str):
        self.analyze_worker = None
        self.progress.setValue(0)
        self.info_label.setText("")
        self._set_state("idle")
        self._refresh_list_status()
        QMessageBox.warning(self, "Analisi non riuscita", message)

    def _on_analysis_done(self, analyses: list, catalogue: list):
        self.analyze_worker = None
        self.catalogue = catalogue
        self.entries = []
        for fa in analyses:
            if fa.error or not fa.pieces:
                self.entries.append((fa.path, None, fa.error or "Nessuna puntata trovata"))
            for piece in fa.pieces:
                self.entries.append((fa.path, piece, ""))
        self._filling = True
        self.table.setRowCount(0)
        for path, piece, error in self.entries:
            self._add_row(path, piece, error)
        self._filling = False
        sure = sum(1 for _, p, _ in self.entries if p is not None and p.sure)
        total = sum(1 for _, p, _ in self.entries if p is not None)
        self.progress.setValue(100)
        self.info_label.setText(
            f"Puntate trovate: {total} — sicure: {sure}, da controllare: {total - sure}"
            + (" (guarda la colonna «Titolo sullo schermo» e correggi)" if total - sure else ""))
        self._set_state("idle")
        self._refresh_list_status()

    # ------------------------------------------------------------------ table
    @staticmethod
    def _ro_item(text: str) -> QTableWidgetItem:
        item = QTableWidgetItem(text)
        item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
        return item

    def _editable_item(self, text: str) -> QTableWidgetItem:
        item = QTableWidgetItem(text)
        item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable | Qt.ItemIsEditable)
        return item

    def _add_row(self, path: Path, piece: dividi.Piece | None, error: str):
        row = self.table.rowCount()
        self.table.insertRow(row)
        check = QTableWidgetItem()
        check.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled if piece is not None else Qt.NoItemFlags)
        check.setCheckState(Qt.Checked if piece is not None and piece.sure else Qt.Unchecked)
        self.table.setItem(row, COL_CHECK, check)
        self.table.setItem(row, COL_FILE, self._ro_item(short_name(path)))
        self.table.item(row, COL_FILE).setToolTip(path.name)
        if piece is None:
            for col in range(COL_START, COL_SHOT + 1):
                self.table.setItem(row, col, self._ro_item(""))
            self.table.item(row, COL_STATUS).setText(f"Errore: {error}")
            return
        self.table.setItem(row, COL_START, self._editable_item(fmt_time(piece.start)))
        self.table.setItem(row, COL_END, self._editable_item(fmt_time(piece.end)))
        self.table.setItem(row, COL_DUR, self._ro_item(fmt_time(piece.duration)))
        self.table.setItem(row, COL_NUM, self._editable_item("" if piece.number is None else str(piece.number)))
        self.table.setItem(row, COL_TITLE, self._editable_item(piece.title))
        self.table.setItem(row, COL_STATUS, self._ro_item(self._status_text(piece)))
        shot = self._ro_item("")
        if piece.thumb is not None and piece.thumb.is_file():
            pix = QPixmap(str(piece.thumb))
            if not pix.isNull():  # only the stretch of the picture where the title is written
                w, h = pix.width(), pix.height()
                strip = pix.copy(QRect(int(w * 0.13), int(h * 0.29), int(w * 0.74), int(h * 0.385)))
                shot.setIcon(QIcon(strip.scaledToWidth(400, Qt.SmoothTransformation)))
        self.table.setItem(row, COL_SHOT, shot)
        bar = QProgressBar()
        bar.setRange(0, 100)
        self.table.setCellWidget(row, COL_PROGRESS, bar)
        self._paint_row(row, piece)

    @staticmethod
    def _status_text(piece: dividi.Piece) -> str:
        if piece.sure:
            return "Sicura"
        reasons = []
        if piece.ambiguous:
            reasons.append("stesso titolo con più numeri in TMDB")
        elif piece.score < titoli.SURE:
            reasons.append("titolo incerto" if piece.title else "titolo non letto")
        if not piece.snapped:
            reasons.append("taglio stimato")
        if piece.duration < dividi.MIN_PIECE:
            reasons.append("pezzo molto breve")
        return "Da controllare: " + ", ".join(reasons) if reasons else "Corretta a mano"

    def _paint_row(self, row: int, piece: dividi.Piece):
        brush = QBrush() if piece.sure else _AMBER
        for col in (COL_NUM, COL_TITLE, COL_STATUS):
            self.table.item(row, col).setBackground(brush)

    def _piece_at(self, row: int) -> dividi.Piece | None:
        return self.entries[row][1] if 0 <= row < len(self.entries) else None

    def _on_item_changed(self, item: QTableWidgetItem):
        if self._filling or item.column() not in COL_EDITABLE:
            return
        row = item.row()
        piece = self._piece_at(row)
        if piece is None:
            return
        self._filling = True
        try:
            if item.column() in (COL_START, COL_END):
                value = parse_time(item.text())
                start = value if item.column() == COL_START else piece.start
                end = value if item.column() == COL_END else piece.end
                path = self.entries[row][0]
                if value is None or not (0 <= start < end) or end - start < 2:
                    self.info_label.setText("Tempo non valido: scrivilo come minuti:secondi, ad esempio 4:51.3.")
                else:
                    piece.start, piece.end = start, end
                    piece.snapped = True
                    self.info_label.setText(f"Taglio di {short_name(path)} corretto a mano.")
                    self._mark_manual(row, piece, title_checked=False)
                item.setText(fmt_time(piece.start if item.column() == COL_START else piece.end))
                self.table.item(row, COL_DUR).setText(fmt_time(piece.duration))
            elif item.column() == COL_NUM:
                text = item.text().strip()
                if text == "":
                    piece.number = None
                elif text.isdigit():
                    piece.number = int(text)
                    ep = next((e for e in self.catalogue if e.number == piece.number), None)
                    if ep is not None and not piece.title:
                        piece.title = ep.title
                        self.table.item(row, COL_TITLE).setText(ep.title)
                else:
                    item.setText("" if piece.number is None else str(piece.number))
                self._mark_manual(row, piece, title_checked=True)
            else:  # title
                piece.title = item.text().strip()
                ep = titoli.find_by_title(piece.title, self.catalogue)
                if ep is not None:
                    piece.number, piece.title = ep.number, ep.title
                    item.setText(ep.title)
                    self.table.item(row, COL_NUM).setText(str(ep.number))
                self._mark_manual(row, piece, title_checked=True)
        finally:
            self._filling = False

    def _mark_manual(self, row: int, piece: dividi.Piece, title_checked: bool):
        """The user corrected something: a title or number they wrote counts as checked by them; a row that is
        then fully in order gets ticked."""
        if title_checked:
            piece.score, piece.ambiguous = max(piece.score, 1.0), False
        self.table.item(row, COL_STATUS).setText("Corretta a mano" if piece.sure else self._status_text(piece))
        if piece.sure:
            self.table.item(row, COL_CHECK).setCheckState(Qt.Checked)
        self._paint_row(row, piece)

    def _on_double_click(self, row: int, col: int):
        piece = self._piece_at(row)
        if col != COL_SHOT or piece is None or piece.thumb is None or not piece.thumb.is_file():
            return
        dialog = QDialog(self)
        dialog.setWindowTitle(f"Titolo letto: {', '.join(piece.read_lines[:3]) or 'nessuno'}")
        layout = QVBoxLayout(dialog)
        label = QLabel()
        pix = QPixmap(str(piece.thumb))
        label.setPixmap(pix.scaledToWidth(min(1100, pix.width()), Qt.SmoothTransformation))
        layout.addWidget(label)
        dialog.exec()

    # ------------------------------------------------------------------ processing
    def _on_start_clicked(self):
        if self.state == "processing":
            self._cancel_processing()
        else:
            self._start_processing()

    def _output_names(self, rows: list[int]) -> dict[int, str]:
        names: dict[int, str] = {}
        seen: dict[str, int] = {}
        for row in rows:
            piece = self.entries[row][1]
            stem = titoli.file_stem(self.season, piece.number, piece.title)
            if stem == "senza titolo":
                stem = f"{short_name(self.entries[row][0])} puntata {row + 1}"
            seen[stem.lower()] = seen.get(stem.lower(), 0) + 1
            names[row] = stem if seen[stem.lower()] == 1 else f"{stem} ({seen[stem.lower()]})"
        return names

    def _start_processing(self):
        rows = [r for r, (_, p, _) in enumerate(self.entries)
                if p is not None and self.table.item(r, COL_CHECK).checkState() == Qt.Checked]
        if not rows:
            QMessageBox.information(self, "Niente da fare", "Seleziona almeno una puntata nella tabella (casella a sinistra).")
            return
        tools = self._tools()
        if tools is None:
            return
        first = self.entries[rows[0]][0]
        output_dir = Path(self.output_edit.text() or (first.parent / "puntate divise"))
        if any(f.resolve().parent == output_dir.resolve() for f in self.files):
            QMessageBox.warning(self, "Cartella di output", "La cartella di output coincide con quella dei file: sceglierne un'altra.")
            return
        names = self._output_names(rows)
        reply = QMessageBox.question(
            self, "Riepilogo prima di avviare",
            f"Puntate da scrivere: {len(rows)}\nCartella di output: {output_dir}\n\n"
            "Ogni puntata viene ricodificata una volta sola; i file originali non vengono toccati. Procedere?",
            QMessageBox.Yes | QMessageBox.Cancel, QMessageBox.Yes)
        if reply != QMessageBox.Yes:
            return
        self._save_settings()
        applog.log_batch_start(
            "Dividi puntate",
            f"qualità {self.quality_combo.currentText()}, preset {self.preset_combo.currentData()}, "
            f"codec {self.codec_combo.currentData()}, cartella di output {output_dir}",
            len(rows), self.parallel_spin.value())
        self.cancel_event = threading.Event()
        self.pool.setMaxThreadCount(self.parallel_spin.value())
        self.started = set()
        self.job_rows = rows
        self.total_count, self.finished_count = len(rows), 0
        self.ok_count = self.skip_count = self.err_count = 0
        self.progress.setValue(0)
        self.info_label.setText("")
        self.row_pct = {r: 0.0 for r in rows}
        self._set_state("processing")
        for row in rows:
            path, piece, _ = self.entries[row]
            self.table.item(row, COL_STATUS).setText("In coda")
            self.table.cellWidget(row, COL_PROGRESS).setValue(0)
            self.pool.start(ProcessTask(
                row, tools[0], tools[1], path, piece.start, piece.end, output_dir / (names[row] + path.suffix),
                self.quality_combo.currentData(), self.preset_combo.currentData(), self.codec_combo.currentData(),
                names[row], self.signals, self.cancel_event))

    def _cancel_processing(self):
        self.cancel_event.set()
        self.pool.clear()
        # Tasks that never started are dropped by pool.clear() without ever reporting back.
        for row in self.job_rows:
            if row not in self.started:
                self.table.item(row, COL_STATUS).setText("Annullato")
                self.row_pct[row] = 1.0
                self.finished_count += 1
        self._update_global()
        self._check_all_done()

    def _on_status(self, row: int, text: str):
        if row < self.table.rowCount():
            self.started.add(row)
            self.table.item(row, COL_STATUS).setText(text)

    def _on_progress(self, row: int, pct: float):
        bar = self.table.cellWidget(row, COL_PROGRESS)
        if bar is not None:
            bar.setValue(int(pct * 100))
        self.row_pct[row] = pct
        item = self.table.item(row, COL_STATUS)
        if item is not None:
            item.setText(f"In elaborazione {int(pct * 100)}%")
        self._update_global()

    def _update_global(self):
        if self.total_count:
            self.progress.setValue(int(sum(self.row_pct.values()) * 100 / self.total_count))

    def _on_finished(self, row: int, result):
        title_item = self.table.item(row, COL_TITLE)
        applog.log_result("Dividi puntate", title_item.text() if title_item else f"riga {row}", result)
        self.finished_count += 1
        self.row_pct[row] = 1.0
        bar = self.table.cellWidget(row, COL_PROGRESS)
        if bar is not None:
            bar.setValue(100)
        if result.skipped:
            self.skip_count += 1
            text = "Saltata (già presente)"
        elif result.success:
            self.ok_count += 1
            text = "Completata" + (f" — {result.note}" if result.note else "")
        elif result.error == "Annullato":
            text = "Annullata"
        else:
            self.err_count += 1
            text = f"Errore: {result.error}"
        item = self.table.item(row, COL_STATUS)
        if item is not None:
            item.setText(text)
        self._update_global()
        self._check_all_done()

    def _check_all_done(self):
        if self.finished_count >= self.total_count:
            self._set_state("idle")
            self.info_label.setText(f"Completate: {self.ok_count}  Saltate: {self.skip_count}  Fallite: {self.err_count}")
            if not self.cancel_event.is_set():
                self.queue_finished.emit()
