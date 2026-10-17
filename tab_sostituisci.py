"""Scheda "Sostituisci sigla": toglie la sigla che c'è già dentro gli episodi (iniziale e/o finale) e ci
mette una nuova, audio e video. Due tempi: "Analizza" trova le vecchie sigle e mostra i risultati,
"Elabora" scrive i nuovi file (niente viene scritto prima che l'utente veda la tabella)."""
from __future__ import annotations

import tempfile
import threading
from pathlib import Path

from PySide6.QtCore import Qt, QObject, QRunnable, QThread, QThreadPool, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QDragEnterEvent, QDropEvent
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QFileDialog, QGridLayout, QGroupBox, QHBoxLayout, QHeaderView, QLabel,
    QLineEdit, QMessageBox, QProgressBar, QPushButton, QSpinBox, QTableWidget, QTableWidgetItem,
    QVBoxLayout, QWidget,
)

import merger
import sostituisci
import utils
from sostituisci import EpisodeAnalysis

PRESETS = [("Veloce", "fast"), ("Bilanciato", "medium"), ("Qualità", "slow")]
AUDIO_MODES = [
    ("Come originale (stesso codec, canali e bitrate dell'episodio)", "original"),
    ("AAC, mantieni i canali originali", "aac_keep"),
    ("AAC stereo 192k (compatto)", "aac_stereo"),
    ("FLAC senza perdita (file più grandi)", "flac"),
]
COL_CHECK, COL_NAME, COL_INTRO, COL_OUTRO, COL_CUT_IN, COL_CUT_OUT, COL_CONF, COL_STATUS, COL_PROGRESS = range(9)


def fmt_time(t: float) -> str:
    minutes, seconds = divmod(t, 60)
    return f"{int(minutes)}:{seconds:04.1f}"


def parse_time(text: str) -> float | None:
    """"1:00.7", "60,7" or "0:01:00.7" -> seconds; None when it can't be read."""
    try:
        parts = [float(p) for p in text.strip().replace(",", ".").split(":")]
    except ValueError:
        return None
    if not parts or len(parts) > 3 or any(p < 0 for p in parts):
        return None
    seconds = 0.0
    for p in parts:
        seconds = seconds * 60 + p
    return seconds


def is_sigla_file(path: Path) -> bool:
    name = path.name.lower()
    return "sigla" in name and ("iniziale" in name or "finale" in name)


class FolderDropEdit(QLineEdit):
    folder_dropped = Signal(str)

    def __init__(self, placeholder: str):
        super().__init__()
        self.setPlaceholderText(placeholder)
        self.setAcceptDrops(True)

    def dragEnterEvent(self, event: QDragEnterEvent):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event: QDropEvent):
        paths = [Path(u.toLocalFile()) for u in event.mimeData().urls()]
        if len(paths) == 1 and paths[0].is_dir():
            self.folder_dropped.emit(str(paths[0]))


class AnalyzeWorker(QThread):
    progress = Signal(float, str)
    done = Signal(list)
    failed = Signal(str)

    def __init__(self, ffmpeg: str, ffprobe: str, episodes: list[Path], intro_cfg, outro_cfg, keep_card: bool = True):
        super().__init__()
        self.ffmpeg, self.ffprobe = ffmpeg, ffprobe
        self.episodes, self.intro_cfg, self.outro_cfg = episodes, intro_cfg, outro_cfg
        self.keep_card = keep_card

    def run(self):
        try:
            result = sostituisci.analizza(
                self.episodes, self.intro_cfg, self.outro_cfg, self.ffmpeg, self.ffprobe,
                progress=lambda f, text: self.progress.emit(f, text),
                cancelled=self.isInterruptionRequested, keep_card=self.keep_card,
            )
        except sostituisci.Annullato:
            return
        except Exception as exc:  # never let the thread die silently
            self.failed.emit(str(exc))
            return
        self.done.emit(result)


class PreviewWorker(QThread):
    ready = Signal(list)
    failed = Signal(str)

    def __init__(self, ffmpeg, ffprobe, ana, new_intro, new_outro, target_dir):
        super().__init__()
        self.args = (ffmpeg, ffprobe, ana, new_intro, new_outro, target_dir)

    def run(self):
        try:
            self.ready.emit([str(p) for p in sostituisci.anteprima_giunzioni(*self.args)])
        except Exception as exc:
            self.failed.emit(str(exc))


class ProcessSignals(QObject):
    progress = Signal(int, float)
    status = Signal(int, str)
    finished = Signal(int, object)


class ProcessTask(QRunnable):
    def __init__(self, row: int, ffmpeg: str, ffprobe: str, ana: EpisodeAnalysis, new_intro, new_outro,
                 output_dir: Path, quality: int, preset: str, audio_mode: str,
                 signals: ProcessSignals, cancel_event: threading.Event):
        super().__init__()
        self.row = row
        self.args = (ffmpeg, ffprobe, ana, new_intro, new_outro, output_dir, quality, preset, audio_mode)
        self.signals, self.cancel_event = signals, cancel_event
        self.setAutoDelete(True)

    def run(self):
        self.signals.status.emit(self.row, "In elaborazione")
        try:
            result = sostituisci.elabora_episodio(
                *self.args,
                progress_cb=lambda pct: self.signals.progress.emit(self.row, pct),
                cancel_event=self.cancel_event,
            )
        except Exception as exc:
            result = merger.MergeResult(output_path=Path(), success=False, error=str(exc))
        self.signals.finished.emit(self.row, result)


class SigleGroup(QGroupBox):
    """One of the two sigle: the new file to put in, and where to find the old one to take out."""

    def __init__(self, title: str, kind: str):
        super().__init__(title)
        self.kind = kind
        self.setCheckable(True)
        self.setChecked(True)
        grid = QGridLayout(self)
        grid.setSpacing(8)

        grid.addWidget(QLabel("Nuova sigla:"), 0, 0)
        self.new_edit = QLineEdit()
        self.new_edit.setPlaceholderText("Il file con la sigla da mettere...")
        grid.addWidget(self.new_edit, 0, 1, 1, 2)
        browse_new = QPushButton("Sfoglia...")
        browse_new.setObjectName("secondary")
        browse_new.clicked.connect(lambda: self._browse(self.new_edit))
        grid.addWidget(browse_new, 0, 3)

        grid.addWidget(QLabel("Vecchia sigla da togliere:"), 1, 0)
        self.how_combo = QComboBox()
        self.how_combo.addItem("Impara dagli episodi", "impara")
        self.how_combo.addItem("Da un file di esempio", "file")
        grid.addWidget(self.how_combo, 1, 1)
        self.old_edit = QLineEdit()
        self.old_edit.setPlaceholderText("Un file con la vecchia sigla (video o audio)...")
        self.old_edit.setEnabled(False)
        grid.addWidget(self.old_edit, 1, 2)
        self.browse_old = QPushButton("Sfoglia...")
        self.browse_old.setObjectName("secondary")
        self.browse_old.setEnabled(False)
        self.browse_old.clicked.connect(lambda: self._browse(self.old_edit))
        grid.addWidget(self.browse_old, 1, 3)
        grid.setColumnStretch(2, 1)
        self.how_combo.currentIndexChanged.connect(self._how_changed)

    def _how_changed(self):
        from_file = self.how_combo.currentData() == "file"
        self.old_edit.setEnabled(from_file)
        self.browse_old.setEnabled(from_file)

    def _browse(self, target: QLineEdit):
        path, _ = QFileDialog.getOpenFileName(self, "Seleziona il file", "", "Video e audio (*.*)")
        if path:
            target.setText(path)

    def active(self) -> bool:
        return self.isChecked()

    def new_path(self) -> Path | None:
        return Path(self.new_edit.text()) if self.new_edit.text() else None

    def old_config(self):
        """None when this sigla is off, "impara", or the sample file."""
        if not self.isChecked():
            return None
        if self.how_combo.currentData() == "impara":
            return "impara"
        return Path(self.old_edit.text()) if self.old_edit.text() else Path()


class SostituisciTab(QWidget):
    def __init__(self, main_window):
        super().__init__()
        self.win = main_window
        self.episodes: list[Path] = []
        self.analyses: list[EpisodeAnalysis] = []
        self.state = "idle"  # "idle" | "analyzing" | "processing"
        self.analyze_worker: AnalyzeWorker | None = None
        self.preview_worker: PreviewWorker | None = None
        self.retired_workers: list[QThread] = []
        self.pool = QThreadPool()
        self.signals = ProcessSignals()
        self.cancel_event = threading.Event()
        self.started: set[int] = set()
        self.finished_count = self.total_count = 0
        self.ok_count = self.skip_count = self.err_count = 0
        self.job_rows: list[int] = []
        self._filling = False  # True while the table is being filled by code (ignores itemChanged)
        self._build_ui()
        self.signals.progress.connect(self._on_progress)
        self.signals.status.connect(self._on_status)
        self.signals.finished.connect(self._on_finished)

    # ------------------------------------------------------------------ UI
    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setSpacing(10)

        top = QGridLayout()
        top.setSpacing(8)
        top.addWidget(QLabel("Cartella episodi:"), 0, 0)
        self.folder_edit = FolderDropEdit("Trascina qui la cartella, oppure sfoglia...")
        self.folder_edit.folder_dropped.connect(self._load_folder)
        top.addWidget(self.folder_edit, 0, 1)
        browse = QPushButton("Sfoglia...")
        browse.setObjectName("secondary")
        browse.clicked.connect(self._browse_folder)
        top.addWidget(browse, 0, 2)
        top.addWidget(QLabel("Cartella output:"), 1, 0)
        self.output_edit = QLineEdit()
        self.output_edit.setPlaceholderText("<cartella episodi>/sigle sostituite")
        top.addWidget(self.output_edit, 1, 1)
        browse_out = QPushButton("Sfoglia...")
        browse_out.setObjectName("secondary")
        browse_out.clicked.connect(self._browse_output)
        top.addWidget(browse_out, 1, 2)
        top_box = QGroupBox("Episodi")
        top_box.setLayout(top)
        root.addWidget(top_box)

        self.intro_group = SigleGroup("Sostituisci la sigla iniziale", "iniziale")
        self.outro_group = SigleGroup("Sostituisci la sigla finale", "finale")
        root.addWidget(self.intro_group)
        root.addWidget(self.outro_group)

        self.card_check = QCheckBox(
            "Conserva la schermata dopo la sigla iniziale (es. il titolo dell'episodio), senza la musica della vecchia sigla")
        self.card_check.setChecked(True)
        self.card_check.setToolTip(
            "Se la vecchia sigla è seguita da una schermata con il titolo dell'episodio, resta quella schermata "
            "(muta). Spegni per togliere tutto fino alla fine della musica.")
        root.addWidget(self.card_check)

        enc = QGridLayout()
        enc.setSpacing(8)
        enc.addWidget(QLabel("Qualità:"), 0, 0)
        self.quality_combo = QComboBox()
        for label, level in sostituisci.QUALITY_LEVELS:
            self.quality_combo.addItem(f"{label}  (CRF {level})", level)
        self.quality_combo.setCurrentIndex(1)
        enc.addWidget(self.quality_combo, 0, 1)
        enc.addWidget(QLabel("Preset:"), 0, 2)
        self.preset_combo = QComboBox()
        for label, value in PRESETS:
            self.preset_combo.addItem(label, value)
        self.preset_combo.setCurrentIndex(2)
        enc.addWidget(self.preset_combo, 0, 3)
        enc.addWidget(QLabel("Elaborazioni parallele:"), 0, 4)
        self.parallel_spin = QSpinBox()
        self.parallel_spin.setRange(1, 4)
        self.parallel_spin.setValue(1)
        enc.addWidget(self.parallel_spin, 0, 5)
        enc.addWidget(QLabel("Audio:"), 1, 0)
        self.audio_combo = QComboBox()
        for label, value in AUDIO_MODES:
            self.audio_combo.addItem(label, value)
        enc.addWidget(self.audio_combo, 1, 1, 1, 5)
        note = QLabel(
            "Il video resta nel codec dell'episodio (H.264, H.265 o AV1; per altri formati si usa H.265) e viene "
            "ricodificato una sola volta, con codifica software. Qualità più alta = file più grande."
        )
        note.setWordWrap(True)
        enc.addWidget(note, 2, 0, 1, 6)
        enc.setColumnStretch(1, 1)
        enc_box = QGroupBox("Codifica")
        enc_box.setLayout(enc)
        root.addWidget(enc_box)

        buttons = QHBoxLayout()
        self.analyze_btn = QPushButton("Analizza")
        self.analyze_btn.clicked.connect(self._on_analyze_clicked)
        buttons.addWidget(self.analyze_btn)
        self.preview_btn = QPushButton("Anteprima giunzioni")
        self.preview_btn.setObjectName("secondary")
        self.preview_btn.clicked.connect(self._preview_selected)
        buttons.addWidget(self.preview_btn)
        buttons.addStretch(1)
        self.start_btn = QPushButton("Elabora")
        self.start_btn.clicked.connect(self._on_start_clicked)
        buttons.addWidget(self.start_btn)
        root.addLayout(buttons)

        self.table = QTableWidget(0, 9)
        self.table.setHorizontalHeaderLabels(
            ["", "Episodio", "Sigla iniziale", "Sigla finale", "Tiene da", "Tiene fino a", "Affidab.", "Stato",
             "Avanzamento"])
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(COL_CHECK, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(COL_NAME, QHeaderView.Stretch)
        for col in (COL_INTRO, COL_OUTRO, COL_CUT_IN, COL_CUT_OUT, COL_CONF, COL_STATUS):
            header.setSectionResizeMode(col, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(COL_PROGRESS, QHeaderView.Fixed)
        self.table.setColumnWidth(COL_PROGRESS, 150)
        self.table.setEditTriggers(QAbstractItemView.DoubleClicked | QAbstractItemView.EditKeyPressed)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.cellDoubleClicked.connect(self._show_row_detail)
        self.table.itemChanged.connect(self._on_item_changed)
        self.table.setToolTip(
            "Doppio clic su «Tiene da» o «Tiene fino a» per correggere a mano i punti di taglio (es. 1:00.7); "
            "doppio clic sul resto della riga per i dettagli.")
        self.table.setMinimumHeight(150)
        root.addWidget(self.table, stretch=1)

        bottom = QHBoxLayout()
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        bottom.addWidget(self.progress, stretch=1)
        self.info_label = QLabel("")
        bottom.addWidget(self.info_label)
        root.addLayout(bottom)
        self._refresh_buttons()

    # ------------------------------------------------------------------ helpers
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
        self.cancel_event.set()
        self.pool.clear()
        self.pool.waitForDone(20000)
        for worker in [self.analyze_worker, self.preview_worker, *self.retired_workers]:
            if worker is not None and worker.isRunning():
                worker.requestInterruption()
                worker.wait(5000)

    def _refresh_buttons(self):
        idle = self.state == "idle"
        self.analyze_btn.setText("Annulla" if self.state == "analyzing" else "Analizza")
        self.analyze_btn.setEnabled(idle or self.state == "analyzing")
        self.start_btn.setText("Stop" if self.state == "processing" else "Elabora")
        self.start_btn.setEnabled(self.state == "processing" or (idle and any(a.ready for a in self.analyses)))
        self.preview_btn.setEnabled(idle and any(a.ready for a in self.analyses))
        for w in (self.folder_edit, self.output_edit, self.quality_combo, self.preset_combo, self.parallel_spin,
                  self.audio_combo, self.card_check):
            w.setEnabled(idle)
        self.table.setEditTriggers(
            (QAbstractItemView.DoubleClicked | QAbstractItemView.EditKeyPressed) if idle
            else QAbstractItemView.NoEditTriggers)
        self.intro_group.setEnabled(idle)
        self.outro_group.setEnabled(idle)
        for name in ("analyze_btn", "start_btn"):
            btn = getattr(self, name)
            btn.setObjectName("danger" if btn.text() in ("Annulla", "Stop") else "")
            btn.style().unpolish(btn)
            btn.style().polish(btn)

    def _set_state(self, state: str):
        self.state = state
        self._refresh_buttons()

    # ------------------------------------------------------------------ folder
    def _browse_folder(self):
        if self.is_busy():
            return
        folder = QFileDialog.getExistingDirectory(self, "Seleziona la cartella con gli episodi")
        if folder:
            self._load_folder(folder)

    def _browse_output(self):
        folder = QFileDialog.getExistingDirectory(self, "Seleziona la cartella di output")
        if folder:
            self.output_edit.setText(folder)

    def _load_folder(self, folder: str):
        if self.is_busy():
            return
        files = utils.list_video_files(Path(folder))
        sigle = [f for f in files if is_sigla_file(f)]
        episodes = [f for f in files if not is_sigla_file(f)]
        if not episodes:
            QMessageBox.warning(self, "Nessun video", "Nessun episodio trovato in questa cartella.")
            return
        self.folder_edit.setText(folder)
        if not self.output_edit.text():
            self.output_edit.setText(str(Path(folder) / "sigle sostituite"))
        # A file like "Sigla iniziale - ..." in the folder is a candidate for the NEW sigla (only when
        # there is exactly one, otherwise the choice is left to the user).
        for group in (self.intro_group, self.outro_group):
            found = [f for f in sigle if group.kind in f.name.lower()]
            if len(found) == 1 and not group.new_edit.text():
                group.new_edit.setText(str(found[0]))
        self.episodes = episodes
        self.analyses = []
        self.table.setRowCount(0)
        for ep in episodes:
            self._add_row(ep)
        self.info_label.setText(f"{len(episodes)} episodi. Premi Analizza per cercare le vecchie sigle.")
        self._refresh_buttons()

    @staticmethod
    def _ro_item(text: str) -> QTableWidgetItem:
        item = QTableWidgetItem(text)
        item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
        return item

    def _add_row(self, ep: Path):
        row = self.table.rowCount()
        self.table.insertRow(row)
        check = QTableWidgetItem()
        check.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled)
        check.setCheckState(Qt.Unchecked)
        self.table.setItem(row, COL_CHECK, check)
        self.table.setItem(row, COL_NAME, self._ro_item(ep.name))
        for col in (COL_INTRO, COL_OUTRO, COL_CUT_IN, COL_CUT_OUT, COL_CONF):
            self.table.setItem(row, col, self._ro_item(""))
        self.table.setItem(row, COL_STATUS, self._ro_item("Da analizzare"))
        bar = QProgressBar()
        bar.setRange(0, 100)
        bar.setValue(0)
        self.table.setCellWidget(row, COL_PROGRESS, bar)

    # ------------------------------------------------------------------ analysis
    def _on_analyze_clicked(self):
        if self.state == "analyzing":
            self._cancel_analysis()
            return
        if not self.episodes:
            QMessageBox.warning(self, "Dati mancanti", "Seleziona prima una cartella con gli episodi.")
            return
        intro_cfg, outro_cfg = self.intro_group.old_config(), self.outro_group.old_config()
        if intro_cfg is None and outro_cfg is None:
            QMessageBox.warning(self, "Dati mancanti", "Attiva almeno una delle due sigle.")
            return
        for cfg in (intro_cfg, outro_cfg):
            if isinstance(cfg, Path) and not cfg.is_file():
                QMessageBox.warning(self, "Dati mancanti", "Seleziona il file di esempio della vecchia sigla.")
                return
        tools = self._tools()
        if tools is None:
            return
        self.analyses = []
        for row in range(self.table.rowCount()):
            self.table.item(row, COL_STATUS).setText("In analisi...")
            self.table.item(row, COL_CHECK).setCheckState(Qt.Unchecked)
            for col in (COL_INTRO, COL_OUTRO, COL_CUT_IN, COL_CUT_OUT, COL_CONF):
                self.table.item(row, col).setText("")
                self.table.item(row, col).setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
        self.progress.setValue(0)
        self.analyze_worker = AnalyzeWorker(tools[0], tools[1], list(self.episodes), intro_cfg, outro_cfg,
                                            self.card_check.isChecked())
        self.analyze_worker.progress.connect(self._on_analysis_progress)
        self.analyze_worker.done.connect(self._on_analysis_done)
        self.analyze_worker.failed.connect(self._on_analysis_failed)
        self.analyze_worker.start()
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
        for row in range(self.table.rowCount()):
            self.table.item(row, COL_STATUS).setText("Da analizzare")
        self.progress.setValue(0)
        self.info_label.setText("Analisi annullata.")
        self._set_state("idle")

    def _on_analysis_progress(self, fraction: float, text: str):
        self.progress.setValue(int(fraction * 100))
        self.info_label.setText(text)

    def _on_analysis_failed(self, message: str):
        self.analyze_worker = None
        for row in range(self.table.rowCount()):
            self.table.item(row, COL_STATUS).setText("Da analizzare")
        self.progress.setValue(0)
        self.info_label.setText("")
        self._set_state("idle")
        QMessageBox.warning(self, "Analisi non riuscita", message)

    def _on_analysis_done(self, analyses: list):
        self.analyze_worker = None
        self.analyses = analyses
        counts = {"Trovata": 0, "Da verificare": 0, "Non trovata": 0, "Errore": 0}
        for row, ana in enumerate(analyses):
            self._fill_row(row, ana)
            counts[ana.status] += 1
        self.progress.setValue(100)
        self.info_label.setText(
            f"Trovate: {counts['Trovata']}  Da verificare: {counts['Da verificare']}  "
            f"Non trovate: {counts['Non trovata']}  Errori: {counts['Errore']}"
        )
        self._set_state("idle")

    def _fill_row(self, row: int, ana: EpisodeAnalysis):
        def side(want: bool, det) -> str:
            if not want:
                return "-"
            if det is None or det.status == "non_trovata":
                return "non trovata"
            return f"{fmt_time(det.start)} → {fmt_time(det.end)}"

        self._filling = True
        self.table.item(row, COL_INTRO).setText(side(ana.want_intro, ana.intro))
        self.table.item(row, COL_OUTRO).setText(side(ana.want_outro, ana.outro))
        self.table.item(row, COL_CONF).setText(str(ana.confidence))
        for col, value in ((COL_CUT_IN, ana.plan.keep_start if ana.plan else None),
                           (COL_CUT_OUT, ana.plan.keep_end if ana.plan else None)):
            cell = self.table.item(row, col)
            cell.setText(fmt_time(value) if value is not None else "")
            cell.setFlags((Qt.ItemIsEnabled | Qt.ItemIsSelectable | Qt.ItemIsEditable) if value is not None
                          else (Qt.ItemIsEnabled | Qt.ItemIsSelectable))
        self._filling = False
        self.table.item(row, COL_STATUS).setText(ana.status if not ana.error else f"Errore: {ana.error}")
        check = self.table.item(row, COL_CHECK)
        if ana.ready:
            check.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled)
            check.setCheckState(Qt.Checked if ana.status == "Trovata" else Qt.Unchecked)
        else:
            check.setFlags(Qt.NoItemFlags)
            check.setCheckState(Qt.Unchecked)

    def _on_item_changed(self, item: QTableWidgetItem):
        """Manual correction of a cut point (the cell holds a time like 1:00.7)."""
        if self._filling or item.column() not in (COL_CUT_IN, COL_CUT_OUT):
            return
        row = item.row()
        ana = self.analyses[row] if row < len(self.analyses) else None
        if ana is None or ana.plan is None:
            return
        plan = ana.plan
        value = parse_time(item.text())
        start = value if item.column() == COL_CUT_IN else plan.keep_start
        end = value if item.column() == COL_CUT_OUT else plan.keep_end
        if value is None or not (0 <= start < end <= ana.duration + 0.5) or end - start < 10:
            self.info_label.setText("Tempo non valido: scrivilo come minuti:secondi, ad esempio 1:00.7.")
        else:
            plan.keep_start, plan.keep_end = start, end
            if ana.want_intro and ana.intro is not None:
                plan.mute_head = max(ana.intro.end - plan.keep_start, 0.0)
            self.info_label.setText(f"Taglio di {ana.episode.name} corretto a mano.")
        self._filling = True
        item.setText(fmt_time(plan.keep_start if item.column() == COL_CUT_IN else plan.keep_end))
        self._filling = False

    def _show_row_detail(self, row: int, col: int):
        if row >= len(self.analyses) or col in (COL_CUT_IN, COL_CUT_OUT):
            return
        ana = self.analyses[row]
        lines = [ana.episode.name, f"Stato: {ana.status}"]
        if ana.error:
            lines.append(f"Errore: {ana.error}")
        if ana.intro is not None and ana.want_intro:
            lines.append(f"Sigla iniziale: {ana.intro}")
        if ana.outro is not None and ana.want_outro:
            lines.append(f"Sigla finale: {ana.outro}")
        if ana.plan is not None:
            lines.append(
                f"Si tiene l'episodio da {fmt_time(ana.plan.keep_start)} a {fmt_time(ana.plan.keep_end)}"
                + (f" (silenzio nei primi {ana.plan.mute_head:.1f} s dopo il taglio iniziale)" if ana.plan.mute_head > 0 else "")
            )
        elif ana.status in ("Trovata", "Da verificare"):
            lines.append("Punti di taglio non determinati.")
        QMessageBox.information(self, "Dettagli", "\n".join(lines))

    # ------------------------------------------------------------------ preview
    def _new_paths_ok(self) -> tuple[Path | None, Path | None] | None:
        """The new sigle to use (None = leave that one alone); None when something is missing."""
        result = []
        for group in (self.intro_group, self.outro_group):
            if not group.active():
                result.append(None)
                continue
            path = group.new_path()
            if path is None or not path.is_file():
                QMessageBox.warning(self, "Dati mancanti", f"Seleziona il file della nuova sigla {group.kind}.")
                return None
            result.append(path)
        return result[0], result[1]

    def _preview_selected(self):
        row = self.table.currentRow()
        if row < 0 or row >= len(self.analyses) or not self.analyses[row].ready:
            QMessageBox.information(self, "Anteprima", "Seleziona nella tabella un episodio analizzato.")
            return
        news = self._new_paths_ok()
        tools = self._tools()
        if news is None or tools is None:
            return
        self.preview_btn.setEnabled(False)
        self.info_label.setText("Preparo l'anteprima...")
        target = Path(tempfile.gettempdir()) / "ClipMerger_anteprima"
        worker = PreviewWorker(tools[0], tools[1], self.analyses[row], news[0], news[1], target)
        worker.ready.connect(self._on_preview_ready)
        worker.failed.connect(self._on_preview_failed)
        self.preview_worker = worker
        worker.start()

    def _on_preview_ready(self, paths: list):
        self.info_label.setText("")
        self._refresh_buttons()
        for path in paths:
            QDesktopServices.openUrl(QUrl.fromLocalFile(path))

    def _on_preview_failed(self, message: str):
        self.info_label.setText("")
        self._refresh_buttons()
        QMessageBox.warning(self, "Anteprima non riuscita", message)

    # ------------------------------------------------------------------ processing
    def _on_start_clicked(self):
        if self.state == "processing":
            self._cancel_processing()
        else:
            self._start_processing()

    def _start_processing(self):
        rows = [r for r, a in enumerate(self.analyses)
                if a.ready and self.table.item(r, COL_CHECK).checkState() == Qt.Checked]
        if not rows:
            QMessageBox.information(self, "Niente da fare", "Seleziona almeno un episodio nella tabella (casella a sinistra).")
            return
        news = self._new_paths_ok()
        tools = self._tools()
        if news is None or tools is None:
            return
        output_dir = Path(self.output_edit.text() or (Path(self.folder_edit.text()) / "sigle sostituite"))
        if any(p.resolve().parent == output_dir.resolve() for p in self.episodes):
            QMessageBox.warning(self, "Cartella di output", "La cartella di output coincide con quella degli episodi: sceglierne un'altra.")
            return
        reply = QMessageBox.question(
            self, "Riepilogo prima di avviare",
            f"Episodi da elaborare: {len(rows)}\nCartella di output: {output_dir}\n\n"
            "Ogni episodio viene ricodificato una volta sola. Procedere?",
            QMessageBox.Yes | QMessageBox.Cancel, QMessageBox.Yes,
        )
        if reply != QMessageBox.Yes:
            return
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
            self.table.item(row, COL_STATUS).setText("In coda")
            self.table.cellWidget(row, COL_PROGRESS).setValue(0)
            self.pool.start(ProcessTask(
                row, tools[0], tools[1], self.analyses[row], news[0], news[1], output_dir,
                self.quality_combo.currentData(), self.preset_combo.currentData(), self.audio_combo.currentData(),
                self.signals, self.cancel_event,
            ))

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
        self.finished_count += 1
        self.row_pct[row] = 1.0
        bar = self.table.cellWidget(row, COL_PROGRESS)
        if bar is not None:
            bar.setValue(100)
        if result.skipped:
            self.skip_count += 1
            text = "Saltato (già presente)"
        elif result.success:
            self.ok_count += 1
            text = "Completato" + (f" — {result.note}" if result.note else "")
        elif result.error == "Annullato":
            text = "Annullato"
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
            self.info_label.setText(
                f"Completati: {self.ok_count}  Saltati: {self.skip_count}  Falliti: {self.err_count}")
