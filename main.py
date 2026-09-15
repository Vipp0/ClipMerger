"""ClipMerger - unisce sigla iniziale e finale a un batch di episodi.

Avvio: python main.py
"""
from __future__ import annotations

import json
import sys
import threading
import time
import urllib.request
from pathlib import Path

from PySide6.QtCore import Qt, QObject, Signal, QRunnable, QThreadPool, QThread, QSettings
from PySide6.QtGui import QDragEnterEvent, QDropEvent, QGuiApplication
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QGridLayout,
    QGroupBox, QLabel, QLineEdit, QPushButton, QFileDialog, QTableWidget,
    QTableWidgetItem, QProgressBar, QComboBox, QRadioButton, QButtonGroup,
    QStackedWidget, QSpinBox, QMessageBox, QCheckBox, QHeaderView, QAbstractItemView,
    QToolButton,
)

import utils
import merger
from merger import MergeSettings, MergeResult

APP_TITLE = "ClipMerger"
APP_VERSION = "0.2.0"
GITHUB_LATEST_RELEASE_API = "https://api.github.com/repos/Vipp0/ClipMerger/releases/latest"
GITHUB_RELEASES_PAGE = "https://github.com/Vipp0/ClipMerger/releases/latest"

PRESET_LABELS = [("Veloce", "fast"), ("Bilanciato", "medium"), ("Qualità", "slow")]
CODEC_LABELS = [("H.264", "h264"), ("H.265 (HEVC)", "h265"), ("AV1", "av1")]

# Theme cycle button: order, monochrome (non-emoji) glyph and tooltip per choice.
THEME_CYCLE = ["auto", "light", "dark"]
THEME_GLYPHS = {"light": "☼", "dark": "☾", "auto": "◐"}
THEME_TOOLTIPS = {
    "light": "Tema: Chiaro (clic per cambiare)",
    "dark": "Tema: Scuro (clic per cambiare)",
    "auto": "Tema: Automatico, segue il sistema (clic per cambiare)",
}

COL_NAME, COL_STATUS, COL_PROGRESS = range(3)

THEMES = {
    "dark": dict(
        bg="#1e1f24", panel="#24252b", border="#33343c", input_bg="#2a2b32",
        input_border="#3a3b44", text="#e8e8ec", muted="#9fa3ad",
        accent="#3a6df0", accent_hover="#4f7ef5", accent_text="#ffffff",
        disabled_bg="#34353d", disabled_text="#7a7c85",
        danger="#c0392b", danger_hover="#d8483a",
        header_bg="#2a2b32", selection_bg="#3a6df0", selection_text="#ffffff",
    ),
    "light": dict(
        bg="#f3f4f6", panel="#ffffff", border="#d7d9de", input_bg="#ffffff",
        input_border="#c7c9d1", text="#1c1d21", muted="#5b5e66",
        accent="#3a6df0", accent_hover="#588bf5", accent_text="#ffffff",
        disabled_bg="#e4e5e9", disabled_text="#9a9ca3",
        danger="#c0392b", danger_hover="#d8483a",
        header_bg="#eef0f3", selection_bg="#3a6df0", selection_text="#ffffff",
    ),
}


def build_stylesheet(theme: str) -> str:
    c = THEMES[theme]
    up_icon = utils.resource_path("assets/spin_up.svg")
    down_icon = utils.resource_path("assets/spin_down.svg")
    return f"""
QMainWindow, QWidget {{ background-color: {c['bg']}; color: {c['text']}; font-size: 13px; }}
QGroupBox {{ background-color: {c['panel']}; border: 1px solid {c['border']}; border-radius: 6px; margin-top: 10px; padding-top: 10px; font-weight: 600; }}
QGroupBox::title {{ subcontrol-origin: margin; left: 10px; padding: 0 4px; color: {c['muted']}; }}
QLineEdit, QComboBox, QSpinBox {{ background-color: {c['input_bg']}; border: 1px solid {c['input_border']}; border-radius: 4px; padding: 5px; color: {c['text']}; }}
QLineEdit:read-only {{ color: {c['muted']}; }}
QComboBox::drop-down {{ border: none; width: 22px; }}
QSpinBox {{ padding-right: 2px; }}
QSpinBox::up-button, QSpinBox::down-button {{
    subcontrol-origin: border; width: 16px; border: none;
    background-color: {c['disabled_bg']};
}}
QSpinBox::up-button {{ subcontrol-position: top right; border-top-right-radius: 4px; margin: 1px 1px 0 0; }}
QSpinBox::down-button {{ subcontrol-position: bottom right; border-bottom-right-radius: 4px; margin: 0 1px 1px 0; }}
QSpinBox::up-button:hover, QSpinBox::down-button:hover {{ background-color: {c['input_border']}; }}
QSpinBox::up-button:pressed, QSpinBox::down-button:pressed {{ background-color: {c['accent']}; }}
QSpinBox::up-arrow {{ image: url({up_icon}); width: 9px; height: 6px; }}
QSpinBox::down-arrow {{ image: url({down_icon}); width: 9px; height: 6px; }}
QComboBox QAbstractItemView {{
    background-color: {c['input_bg']}; color: {c['text']};
    border: 1px solid {c['input_border']};
    selection-background-color: {c['selection_bg']}; selection-color: {c['selection_text']};
    outline: none;
}}
QPushButton {{ background-color: {c['accent']}; border: none; border-radius: 4px; padding: 6px 14px; color: {c['accent_text']}; font-weight: 600; }}
QPushButton:hover {{ background-color: {c['accent_hover']}; }}
QPushButton:disabled {{ background-color: {c['disabled_bg']}; color: {c['disabled_text']}; }}
QPushButton#secondary {{ background-color: {c['disabled_bg']}; color: {c['text']}; }}
QPushButton#secondary:hover {{ background-color: {c['input_border']}; }}
QPushButton#danger {{ background-color: {c['danger']}; }}
QPushButton#danger:hover {{ background-color: {c['danger_hover']}; }}
QPushButton#danger:disabled {{ background-color: {c['disabled_bg']}; color: {c['disabled_text']}; }}
QTableWidget {{ background-color: {c['panel']}; border: 1px solid {c['border']}; gridline-color: {c['border']}; color: {c['text']}; }}
QHeaderView::section {{ background-color: {c['header_bg']}; color: {c['muted']}; border: none; padding: 6px; }}
QProgressBar {{ border: 1px solid {c['input_border']}; border-radius: 4px; text-align: center; background-color: {c['input_bg']}; color: {c['text']}; }}
QProgressBar::chunk {{ background-color: {c['accent']}; border-radius: 3px; }}
QRadioButton, QCheckBox, QLabel {{ color: {c['text']}; background: transparent; }}
QRadioButton:disabled, QCheckBox:disabled {{ color: {c['disabled_text']}; }}
QRadioButton::indicator, QCheckBox::indicator {{ width: 14px; height: 14px; }}
QRadioButton::indicator {{ border-radius: 8px; }}
QCheckBox::indicator {{ border-radius: 3px; }}
QRadioButton::indicator:unchecked, QCheckBox::indicator:unchecked {{
    border: 1px solid {c['muted']}; background-color: {c['input_bg']};
}}
QRadioButton::indicator:checked, QCheckBox::indicator:checked {{
    border: 1px solid {c['accent']}; background-color: {c['accent']};
}}
QRadioButton::indicator:disabled, QCheckBox::indicator:disabled {{
    border: 1px solid {c['disabled_bg']}; background-color: {c['disabled_bg']};
}}
QToolTip {{ background-color: {c['panel']}; color: {c['text']}; border: 1px solid {c['border']}; }}
QToolButton#themeToggle {{
    background-color: {c['input_bg']}; border: 1px solid {c['input_border']};
    border-radius: 6px; padding: 3px 8px; font-size: 14px; color: {c['text']};
    min-width: 20px;
}}
QToolButton#themeToggle:hover {{ background-color: {c['disabled_bg']}; }}
QToolButton#themeToggle:pressed {{ background-color: {c['input_border']}; }}
"""


class DropLineEdit(QLineEdit):
    """Single-file drop target (sigla iniziale/finale)."""

    file_dropped = Signal(str)

    def __init__(self, placeholder: str):
        super().__init__()
        self.setReadOnly(True)
        self.setPlaceholderText(placeholder)
        self.setAcceptDrops(True)

    def dragEnterEvent(self, event: QDragEnterEvent):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dragMoveEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event: QDropEvent):
        urls = event.mimeData().urls()
        if urls:
            path = Path(urls[0].toLocalFile())
            if path.is_file():
                self.file_dropped.emit(str(path))


class DropFolderLineEdit(QLineEdit):
    """Folder drop target (cartella puntate)."""

    folder_dropped = Signal(str)

    def __init__(self, placeholder: str):
        super().__init__()
        self.setReadOnly(True)
        self.setPlaceholderText(placeholder)
        self.setAcceptDrops(True)

    def dragEnterEvent(self, event: QDragEnterEvent):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dragMoveEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event: QDropEvent):
        urls = event.mimeData().urls()
        if urls:
            path = Path(urls[0].toLocalFile())
            if path.is_dir():
                self.folder_dropped.emit(str(path))


class DropTableWidget(QTableWidget):
    """Video queue table; accepts a dropped folder or a set of video files."""

    folder_dropped = Signal(str)
    files_dropped = Signal(list)

    def __init__(self):
        super().__init__(0, 3)
        self.setAcceptDrops(True)
        self.viewport().setAcceptDrops(True)
        self.setDragDropMode(QAbstractItemView.DropOnly)
        self.setHorizontalHeaderLabels(["File", "Stato", "Avanzamento"])
        self.horizontalHeader().setSectionResizeMode(COL_NAME, QHeaderView.Stretch)
        self.horizontalHeader().setSectionResizeMode(COL_STATUS, QHeaderView.ResizeToContents)
        self.horizontalHeader().setSectionResizeMode(COL_PROGRESS, QHeaderView.Fixed)
        self.setColumnWidth(COL_PROGRESS, 160)
        self.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.setSelectionBehavior(QAbstractItemView.SelectRows)

    def dragEnterEvent(self, event: QDragEnterEvent):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dragMoveEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event: QDropEvent):
        paths = [Path(u.toLocalFile()) for u in event.mimeData().urls()]
        if len(paths) == 1 and paths[0].is_dir():
            self.folder_dropped.emit(str(paths[0]))
        else:
            files = [p for p in paths if p.is_file() and p.suffix.lower() in utils.VIDEO_EXTENSIONS]
            if files:
                self.files_dropped.emit(files)


class MergeSignals(QObject):
    progress = Signal(int, float)
    status = Signal(int, str)
    finished = Signal(int, object)


class MergeTask(QRunnable):
    def __init__(self, row: int, intro: Path, episode: Path, outro: Path, output: Path,
                 settings: MergeSettings, ffmpeg_path: str, ffprobe_path: str,
                 signals: MergeSignals, cancel_event: threading.Event):
        super().__init__()
        self.row = row
        self.intro, self.episode, self.outro, self.output = intro, episode, outro, output
        self.settings = settings
        self.ffmpeg_path, self.ffprobe_path = ffmpeg_path, ffprobe_path
        self.signals = signals
        self.cancel_event = cancel_event
        self.setAutoDelete(True)

    def run(self):
        self.signals.status.emit(self.row, "In elaborazione")
        try:
            result = merger.merge_episode(
                self.ffmpeg_path, self.ffprobe_path,
                self.intro, self.episode, self.outro, self.output,
                self.settings,
                progress_cb=lambda pct: self.signals.progress.emit(self.row, pct),
                cancel_event=self.cancel_event,
            )
        except Exception as exc:  # defensive: never let a worker thread crash silently
            result = MergeResult(output_path=self.output, success=False, error=str(exc))
        self.signals.finished.emit(self.row, result)


class HwDetectWorker(QThread):
    done = Signal(dict)

    def __init__(self, ffmpeg_path: str):
        super().__init__()
        self.ffmpeg_path = ffmpeg_path

    def run(self):
        result = {}
        for _, codec in CODEC_LABELS:
            result[codec] = utils.detect_hw_encoder(self.ffmpeg_path, codec)
        self.done.emit(result)


class UpdateCheckWorker(QThread):
    """Queries the GitHub API for the latest release tag. Never raises: any
    network/parsing failure just results in an empty string (silently ignored)."""

    checked = Signal(str)

    def run(self):
        tag = ""
        try:
            req = urllib.request.Request(
                GITHUB_LATEST_RELEASE_API, headers={"User-Agent": "ClipMerger"},
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            tag = data.get("tag_name", "") or ""
        except Exception:
            tag = ""
        self.checked.emit(tag)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(APP_TITLE)

        self.qsettings = QSettings("ClipMerger", "ClipMerger")
        saved_geometry = self.qsettings.value("window_geometry")
        if saved_geometry is not None:
            self.restoreGeometry(saved_geometry)
        else:
            self.resize(980, 640)

        self.ffmpeg_status = utils.check_ffmpeg()
        self.hw_encoders: dict[str, str | None] = {c: None for _, c in CODEC_LABELS}
        self.rows: list[dict] = []  # {path, output}
        self.pool = QThreadPool()
        self.signals = MergeSignals()
        self.cancel_event = threading.Event()
        self.running = False

        self.theme_choice = self.qsettings.value("theme", "auto")
        if self.theme_choice not in ("light", "dark", "auto"):
            self.theme_choice = "auto"

        self._build_ui()
        self._wire_signals()
        self._apply_theme(self._resolve_theme(self.theme_choice))

        if not self.ffmpeg_status.ok:
            QMessageBox.critical(
                self, "ffmpeg non trovato",
                "ffmpeg e/o ffprobe non sono stati trovati nel PATH di sistema.\n\n"
                "Installa ffmpeg (https://ffmpeg.org/download.html) e assicurati che "
                "la cartella 'bin' sia inclusa nella variabile d'ambiente PATH, poi "
                "riavvia il programma.",
            )
            self.start_btn.setEnabled(False)
        else:
            self.hw_thread = HwDetectWorker(self.ffmpeg_status.ffmpeg_path)
            self.hw_thread.done.connect(self._on_hw_detected)
            self.hw_thread.start()

        self.update_thread = UpdateCheckWorker()
        self.update_thread.checked.connect(self._on_update_checked)
        self.update_thread.start()

    # ---------------------------------------------------------------- UI build
    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setSpacing(10)

        # Theme toggle: single minimal button, cycles auto -> light -> dark -> auto...
        theme_row = QHBoxLayout()

        self.update_label = QLabel("")
        self.update_label.setOpenExternalLinks(True)
        self.update_label.hide()
        theme_row.addWidget(self.update_label)

        theme_row.addStretch(1)

        self.theme_toggle_btn = QToolButton()
        self.theme_toggle_btn.setObjectName("themeToggle")
        self._refresh_theme_toggle()
        theme_row.addWidget(self.theme_toggle_btn)
        root.addLayout(theme_row)

        # Sorgente / sigle / output
        top_grid = QGridLayout()
        top_grid.setSpacing(8)

        top_grid.addWidget(QLabel("Cartella puntate:"), 0, 0)
        self.folder_edit = DropFolderLineEdit("Trascina qui la cartella, oppure sfoglia...")
        top_grid.addWidget(self.folder_edit, 0, 1)
        browse_folder_btn = QPushButton("Sfoglia...")
        browse_folder_btn.setObjectName("secondary")
        browse_folder_btn.clicked.connect(self._browse_folder)
        top_grid.addWidget(browse_folder_btn, 0, 2)

        top_grid.addWidget(QLabel("Sigla iniziale:"), 1, 0)
        self.intro_edit = DropLineEdit("Trascina qui il file, oppure sfoglia...")
        top_grid.addWidget(self.intro_edit, 1, 1)
        browse_intro_btn = QPushButton("Sfoglia...")
        browse_intro_btn.setObjectName("secondary")
        browse_intro_btn.clicked.connect(lambda: self._browse_file(self.intro_edit))
        top_grid.addWidget(browse_intro_btn, 1, 2)

        top_grid.addWidget(QLabel("Sigla finale:"), 2, 0)
        self.outro_edit = DropLineEdit("Trascina qui il file, oppure sfoglia...")
        top_grid.addWidget(self.outro_edit, 2, 1)
        browse_outro_btn = QPushButton("Sfoglia...")
        browse_outro_btn.setObjectName("secondary")
        browse_outro_btn.clicked.connect(lambda: self._browse_file(self.outro_edit))
        top_grid.addWidget(browse_outro_btn, 2, 2)

        top_grid.addWidget(QLabel("Cartella output:"), 3, 0)
        self.output_edit = QLineEdit()
        self.output_edit.setPlaceholderText("<cartella puntate>/output")
        top_grid.addWidget(self.output_edit, 3, 1)
        browse_output_btn = QPushButton("Sfoglia...")
        browse_output_btn.setObjectName("secondary")
        browse_output_btn.clicked.connect(self._browse_output)
        top_grid.addWidget(browse_output_btn, 3, 2)

        top_box = QGroupBox("Sorgente, sigle e output")
        top_box.setLayout(top_grid)
        root.addWidget(top_box)

        # Encoding settings
        enc_grid = QGridLayout()
        enc_grid.setSpacing(8)

        enc_grid.addWidget(QLabel("Codec:"), 0, 0)
        self.codec_combo = QComboBox()
        for label, _ in CODEC_LABELS:
            self.codec_combo.addItem(label)
        enc_grid.addWidget(self.codec_combo, 0, 1)

        self.gpu_check = QCheckBox("Usa GPU se disponibile (rilevamento in corso...)")
        self.gpu_check.setEnabled(False)
        enc_grid.addWidget(self.gpu_check, 0, 2, 1, 2)

        enc_grid.addWidget(QLabel("Preset:"), 1, 0)
        self.preset_combo = QComboBox()
        for label, _ in PRESET_LABELS:
            self.preset_combo.addItem(label)
        self.preset_combo.setCurrentIndex(1)
        enc_grid.addWidget(self.preset_combo, 1, 1)

        enc_grid.addWidget(QLabel("Elaborazioni parallele:"), 1, 2)
        self.parallel_spin = QSpinBox()
        self.parallel_spin.setRange(1, 8)
        self.parallel_spin.setValue(1)
        self.parallel_spin.setFixedWidth(56)
        self.parallel_spin.setAlignment(Qt.AlignCenter)
        enc_grid.addWidget(self.parallel_spin, 1, 3, Qt.AlignLeft)

        enc_grid.addWidget(QLabel("Bitrate:"), 2, 0)
        bitrate_row = QHBoxLayout()
        self.crf_radio = QRadioButton("Qualità (CRF)")
        self.cbr_radio = QRadioButton("Bitrate costante")
        self.orig_radio = QRadioButton("Come originale")
        self.crf_radio.setChecked(True)
        self.bitrate_group = QButtonGroup(self)
        for i, btn in enumerate((self.crf_radio, self.cbr_radio, self.orig_radio)):
            self.bitrate_group.addButton(btn, i)
            bitrate_row.addWidget(btn)

        self.bitrate_stack = QStackedWidget()
        self.crf_spin = QSpinBox()
        self.crf_spin.setRange(0, 51)
        self.crf_spin.setValue(20)
        self.crf_spin.setFixedWidth(56)
        self.crf_spin.setAlignment(Qt.AlignCenter)
        crf_wrap = QWidget()
        crf_l = QHBoxLayout(crf_wrap)
        crf_l.setContentsMargins(0, 0, 0, 0)
        crf_l.addWidget(QLabel("CRF:"))
        crf_l.addWidget(self.crf_spin)
        self.bitrate_stack.addWidget(crf_wrap)

        self.cbr_spin = QSpinBox()
        self.cbr_spin.setRange(100, 50000)
        self.cbr_spin.setValue(2000)
        self.cbr_spin.setSuffix(" kbit/s")
        self.cbr_spin.setFixedWidth(110)
        self.cbr_spin.setAlignment(Qt.AlignCenter)
        cbr_wrap = QWidget()
        cbr_l = QHBoxLayout(cbr_wrap)
        cbr_l.setContentsMargins(0, 0, 0, 0)
        cbr_l.addWidget(QLabel("Bitrate:"))
        cbr_l.addWidget(self.cbr_spin)
        self.bitrate_stack.addWidget(cbr_wrap)

        orig_label = QLabel("Verrà usato il bitrate video della puntata originale.")
        orig_label.setWordWrap(True)
        orig_wrap = QWidget()
        orig_l = QHBoxLayout(orig_wrap)
        orig_l.setContentsMargins(0, 0, 0, 0)
        orig_l.addWidget(orig_label)
        self.bitrate_stack.addWidget(orig_wrap)

        bitrate_row.addSpacing(12)
        bitrate_row.addWidget(self.bitrate_stack)
        bitrate_row.addStretch(1)
        enc_grid.addLayout(bitrate_row, 2, 1, 1, 3)

        self.tune_check = QCheckBox("Ottimizza per cartoni animati (tune animation, solo codifica software H.264/H.265)")
        enc_grid.addWidget(self.tune_check, 3, 0, 1, 4)

        self.two_pass_check = QCheckBox(
            "Codifica a 2 passaggi (qualità migliore a parità di dimensione, più lento — solo software, bitrate costante/originale)"
        )
        enc_grid.addWidget(self.two_pass_check, 4, 0, 1, 4)

        enc_box = QGroupBox("Codifica")
        enc_box.setLayout(enc_grid)
        root.addWidget(enc_box)

        self._update_tune_checkbox()
        self._update_two_pass_checkbox()

        # Queue table
        self.table = DropTableWidget()
        root.addWidget(self.table, stretch=1)

        # Bottom bar
        bottom = QHBoxLayout()
        self.global_progress = QProgressBar()
        self.global_progress.setRange(0, 100)
        bottom.addWidget(self.global_progress, stretch=1)

        self.eta_label = QLabel("")
        bottom.addWidget(self.eta_label)

        self.summary_label = QLabel("")
        bottom.addWidget(self.summary_label)

        self.reset_btn = QPushButton("Reset")
        self.reset_btn.setObjectName("secondary")
        bottom.addWidget(self.reset_btn)

        self.cancel_btn = QPushButton("Annulla")
        self.cancel_btn.setObjectName("danger")
        self.cancel_btn.setEnabled(False)
        bottom.addWidget(self.cancel_btn)

        self.start_btn = QPushButton("Avvia")
        bottom.addWidget(self.start_btn)

        root.addLayout(bottom)

    def _wire_signals(self):
        self.theme_toggle_btn.clicked.connect(self._cycle_theme)
        QGuiApplication.styleHints().colorSchemeChanged.connect(self._on_system_theme_changed)
        self.table.folder_dropped.connect(self._load_folder)
        self.folder_edit.folder_dropped.connect(self._load_folder)
        self.table.files_dropped.connect(self._load_files)
        self.intro_edit.file_dropped.connect(self.intro_edit.setText)
        self.outro_edit.file_dropped.connect(self.outro_edit.setText)
        self.codec_combo.currentIndexChanged.connect(self._update_gpu_checkbox)
        self.codec_combo.currentIndexChanged.connect(self._update_tune_checkbox)
        self.codec_combo.currentIndexChanged.connect(self._update_two_pass_checkbox)
        self.gpu_check.toggled.connect(self._update_tune_checkbox)
        self.gpu_check.toggled.connect(self._update_two_pass_checkbox)
        self.bitrate_group.idClicked.connect(self.bitrate_stack.setCurrentIndex)
        self.bitrate_group.idClicked.connect(self._update_two_pass_checkbox)
        self.table.cellDoubleClicked.connect(self._show_row_detail)
        self.start_btn.clicked.connect(self._start_batch)
        self.cancel_btn.clicked.connect(self._cancel_batch)
        self.reset_btn.clicked.connect(self._reset_all)
        self.signals.progress.connect(self._on_progress)
        self.signals.status.connect(self._on_status)
        self.signals.finished.connect(self._on_finished)

    def _resolve_theme(self, choice: str) -> str:
        if choice != "auto":
            return choice
        scheme = QGuiApplication.styleHints().colorScheme()
        return "dark" if scheme == Qt.ColorScheme.Dark else "light"

    def _apply_theme(self, theme: str):
        self.theme = theme
        app = QApplication.instance()
        app.setStyleSheet(build_stylesheet(theme))

    def _on_theme_choice_changed(self, choice: str):
        self.theme_choice = choice
        self._apply_theme(self._resolve_theme(choice))
        self.qsettings.setValue("theme", choice)
        self._refresh_theme_toggle()

    def _cycle_theme(self):
        next_choice = THEME_CYCLE[(THEME_CYCLE.index(self.theme_choice) + 1) % len(THEME_CYCLE)]
        self._on_theme_choice_changed(next_choice)

    def _refresh_theme_toggle(self):
        self.theme_toggle_btn.setText(THEME_GLYPHS[self.theme_choice])
        self.theme_toggle_btn.setToolTip(THEME_TOOLTIPS[self.theme_choice])

    def _on_system_theme_changed(self, *_args):
        if self.theme_choice == "auto":
            self._apply_theme(self._resolve_theme("auto"))

    def closeEvent(self, event):
        self.qsettings.setValue("window_geometry", self.saveGeometry())
        super().closeEvent(event)

    # ---------------------------------------------------------------- helpers
    def _current_codec(self) -> str:
        return CODEC_LABELS[self.codec_combo.currentIndex()][1]

    def _current_preset(self) -> str:
        return PRESET_LABELS[self.preset_combo.currentIndex()][1]

    def _on_hw_detected(self, result: dict):
        self.hw_encoders = result
        self._update_gpu_checkbox()

    def _on_update_checked(self, latest_tag: str):
        if not latest_tag:
            return
        latest = latest_tag.lstrip("vV")
        if latest and latest != APP_VERSION:
            self.update_label.setText(
                f'<a href="{GITHUB_RELEASES_PAGE}">Nuova versione disponibile: {latest_tag}</a>'
            )
            self.update_label.show()

    def _update_gpu_checkbox(self):
        if not hasattr(self, "hw_thread"):
            return
        if self.hw_thread.isRunning():
            return
        codec = self._current_codec()
        encoder = self.hw_encoders.get(codec)
        if encoder:
            self.gpu_check.setEnabled(True)
            self.gpu_check.setText(f"Usa GPU se disponibile (rilevato: {encoder})")
        else:
            self.gpu_check.setEnabled(False)
            self.gpu_check.setChecked(False)
            self.gpu_check.setText("Usa GPU se disponibile (nessun encoder hardware rilevato)")
        self._update_tune_checkbox()
        self._update_two_pass_checkbox()

    def _update_tune_checkbox(self):
        if not hasattr(self, "tune_check"):
            return
        using_hw = self.gpu_check.isChecked() and self.gpu_check.isEnabled()
        eligible = self._current_codec() in ("h264", "h265") and not using_hw
        self.tune_check.setEnabled(eligible)
        if not eligible:
            self.tune_check.setChecked(False)

    def _update_two_pass_checkbox(self):
        if not hasattr(self, "two_pass_check"):
            return
        using_hw = self.gpu_check.isChecked() and self.gpu_check.isEnabled()
        eligible = (not using_hw) and not self.crf_radio.isChecked()
        self.two_pass_check.setEnabled(eligible)
        if not eligible:
            self.two_pass_check.setChecked(False)

    def _browse_folder(self):
        folder = QFileDialog.getExistingDirectory(self, "Seleziona cartella puntate")
        if folder:
            self._load_folder(folder)

    def _load_folder(self, folder: str):
        self.folder_edit.setText(folder)
        files = utils.list_video_files(Path(folder))
        if not files:
            QMessageBox.warning(self, "Nessun video", "Nessun file video trovato in questa cartella.")
            return
        self._load_files(files)
        if not self.output_edit.text():
            self.output_edit.setText(str(Path(folder) / "output"))

    def _load_files(self, files: list[Path]):
        files = self._auto_assign_sigle(files)
        self.rows = []
        self.table.setRowCount(0)
        for f in files:
            self._add_row(f)

    def _auto_assign_sigle(self, files: list[Path]) -> list[Path]:
        """Pull out files whose name contains "sigla iniziale"/"sigla finale" and
        assign them straight to the intro/outro fields, instead of queueing them."""
        remaining = []
        for f in files:
            name = f.name.lower()
            if "sigla iniziale" in name:
                self.intro_edit.setText(str(f))
            elif "sigla finale" in name:
                self.outro_edit.setText(str(f))
            else:
                remaining.append(f)
        return remaining

    def _add_row(self, path: Path):
        row = self.table.rowCount()
        self.table.insertRow(row)
        self.table.setItem(row, COL_NAME, QTableWidgetItem(path.name))
        self.table.setItem(row, COL_STATUS, QTableWidgetItem("In coda"))
        bar = QProgressBar()
        bar.setRange(0, 100)
        bar.setValue(0)
        self.table.setCellWidget(row, COL_PROGRESS, bar)
        self.rows.append({"path": path, "output": None, "last_status": "In coda"})

    def _browse_file(self, target: DropLineEdit):
        path, _ = QFileDialog.getOpenFileName(self, "Seleziona video", "", "Video (*.*)")
        if path:
            target.setText(path)

    def _browse_output(self):
        folder = QFileDialog.getExistingDirectory(self, "Seleziona cartella output")
        if folder:
            self.output_edit.setText(folder)

    def _set_row_status(self, row: int, status: str):
        self.table.item(row, COL_STATUS).setText(status)
        if row < len(self.rows):
            self.rows[row]["last_status"] = status

    def _set_row_progress(self, row: int, pct: float):
        bar: QProgressBar = self.table.cellWidget(row, COL_PROGRESS)
        bar.setValue(int(pct * 100))

    def _show_row_detail(self, row: int, _column: int):
        if row >= len(self.rows):
            return
        name = self.rows[row]["path"].name
        status = self.rows[row].get("last_status", "")
        QMessageBox.information(self, name, status or "Nessun dettaglio disponibile.")

    # ---------------------------------------------------------------- batch run
    def _validate(self) -> str | None:
        if not self.rows:
            return "Seleziona prima una cartella con dei video."
        if not self.intro_edit.text():
            return "Seleziona il file della sigla iniziale."
        if not self.outro_edit.text():
            return "Seleziona il file della sigla finale."
        if not self.output_edit.text():
            return "Seleziona la cartella di output."
        return None

    def _build_preflight_summary(self) -> tuple[str, str]:
        ffprobe = self.ffmpeg_status.ffprobe_path
        lines = [f"Episodi in coda: {len(self.rows)}"]
        warnings = []

        try:
            intro = merger.probe(ffprobe, Path(self.intro_edit.text()))
            outro = merger.probe(ffprobe, Path(self.outro_edit.text()))
            lines.append(
                f"Sigla iniziale: {utils.format_duration(intro.duration)}  ·  "
                f"Sigla finale: {utils.format_duration(outro.duration)}"
            )
        except merger.MergeError as exc:
            warnings.append(f"Impossibile analizzare le sigle: {exc}")

        resolutions = set()
        durations = []
        for item in self.rows:
            try:
                info = merger.probe(ffprobe, item["path"])
            except merger.MergeError as exc:
                warnings.append(f"{item['path'].name}: {exc}")
                continue
            resolutions.add((info.width, info.height))
            durations.append(info.duration)

        if durations:
            lines.append(
                f"Durata episodi: da {utils.format_duration(min(durations))} "
                f"a {utils.format_duration(max(durations))}"
            )
        if resolutions:
            res_str = ", ".join(f"{w}x{h}" for w, h in sorted(resolutions))
            lines.append(f"Risoluzioni rilevate: {res_str}")
            if len(resolutions) > 1:
                warnings.append(
                    "Gli episodi non hanno tutti la stessa risoluzione "
                    "(verranno comunque adattati singolarmente)."
                )

        return "\n".join(lines), "\n".join(warnings)

    def _start_batch(self):
        error = self._validate()
        if error:
            QMessageBox.warning(self, "Dati mancanti", error)
            return

        summary, warnings = self._build_preflight_summary()
        message = summary + (f"\n\nAvvisi:\n{warnings}" if warnings else "")
        reply = QMessageBox.question(
            self, "Riepilogo prima di avviare", message,
            QMessageBox.Yes | QMessageBox.Cancel, QMessageBox.Yes,
        )
        if reply != QMessageBox.Yes:
            return

        codec = self._current_codec()
        use_gpu = self.gpu_check.isChecked() and self.gpu_check.isEnabled()
        encoder = self.hw_encoders.get(codec) if use_gpu else None
        is_hardware = bool(encoder)
        if not encoder:
            encoder = utils.CODEC_ENCODERS[codec][0]

        if self.crf_radio.isChecked():
            bitrate_mode = "crf"
        elif self.cbr_radio.isChecked():
            bitrate_mode = "cbr"
        else:
            bitrate_mode = "original"

        settings = MergeSettings(
            codec=codec, encoder=encoder, is_hardware=is_hardware,
            preset=self._current_preset(), bitrate_mode=bitrate_mode,
            crf=self.crf_spin.value(), cbr_kbps=self.cbr_spin.value(),
            tune_animation=self.tune_check.isChecked() and self.tune_check.isEnabled(),
            two_pass=self.two_pass_check.isChecked() and self.two_pass_check.isEnabled(),
        )

        output_dir = Path(self.output_edit.text())
        try:
            output_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            QMessageBox.critical(self, "Errore output", f"Impossibile creare la cartella output: {exc}")
            return

        intro_path = Path(self.intro_edit.text())
        outro_path = Path(self.outro_edit.text())

        self.cancel_event = threading.Event()
        self.pool.setMaxThreadCount(self.parallel_spin.value())
        self._set_controls_enabled(False)
        self.running = True
        self.finished_count = 0
        self.total_count = len(self.rows)
        self.ok_count = self.skip_count = self.err_count = 0
        self.summary_label.setText("")
        self.global_progress.setValue(0)
        self.batch_start_time = time.monotonic()
        self.row_pct = {row: 0.0 for row in range(self.total_count)}
        self.row_start_time = {}
        self.eta_label.setText("")

        for row, item in enumerate(self.rows):
            episode_path = item["path"]
            output_path = output_dir / episode_path.name
            item["output"] = output_path
            self._set_row_status(row, "In coda")
            self._set_row_progress(row, 0.0)
            task = MergeTask(
                row, intro_path, episode_path, outro_path, output_path,
                settings, self.ffmpeg_status.ffmpeg_path, self.ffmpeg_status.ffprobe_path,
                self.signals, self.cancel_event,
            )
            self.pool.start(task)

    def _cancel_batch(self):
        self.cancel_event.set()
        self.pool.clear()
        self.cancel_btn.setEnabled(False)

    def _set_controls_enabled(self, enabled: bool):
        self.start_btn.setEnabled(enabled)
        self.cancel_btn.setEnabled(not enabled)
        self.reset_btn.setEnabled(enabled)
        for w in (self.codec_combo, self.gpu_check, self.preset_combo, self.parallel_spin,
                  self.crf_radio, self.cbr_radio, self.orig_radio, self.crf_spin, self.cbr_spin,
                  self.tune_check, self.two_pass_check):
            w.setEnabled(enabled)
        if enabled:
            self._update_gpu_checkbox()

    def _reset_all(self):
        self.folder_edit.clear()
        self.output_edit.clear()
        self.intro_edit.clear()
        self.outro_edit.clear()
        self.rows = []
        self.table.setRowCount(0)
        self.global_progress.setValue(0)
        self.summary_label.setText("")
        self.eta_label.setText("")

    def _on_progress(self, row: int, pct: float):
        self._set_row_progress(row, pct)
        self.row_pct[row] = pct
        start = self.row_start_time.get(row)
        if start is not None and pct > 0.02:
            elapsed = time.monotonic() - start
            remaining = elapsed * (1 - pct) / pct
            self._set_row_status(row, f"In elaborazione (~{utils.format_duration(remaining)} rimanenti)")
        self._update_batch_eta()

    def _on_status(self, row: int, status: str):
        self._set_row_status(row, status)
        if status == "In elaborazione":
            self.row_start_time[row] = time.monotonic()

    def _update_batch_eta(self):
        if not self.total_count:
            return
        overall = sum(self.row_pct.values()) / self.total_count
        self.global_progress.setValue(int(overall * 100))
        if overall > 0.02:
            elapsed = time.monotonic() - self.batch_start_time
            remaining = elapsed * (1 - overall) / overall
            self.eta_label.setText(f"Tempo rimanente stimato: {utils.format_duration(remaining)}")

    def _on_finished(self, row: int, result: MergeResult):
        self.row_pct[row] = 1.0
        if result.skipped:
            self._set_row_status(row, "Già presente, saltato")
            self._set_row_progress(row, 1.0)
            self.skip_count += 1
        elif result.success:
            self._set_row_status(row, "Completato")
            self._set_row_progress(row, 1.0)
            self.ok_count += 1
        else:
            self._set_row_status(row, f"Errore: {result.error}")
            self.err_count += 1

        self.finished_count += 1
        self._update_batch_eta()

        if self.finished_count >= self.total_count:
            self.running = False
            self._set_controls_enabled(True)
            self.global_progress.setValue(100)
            self.eta_label.setText("")
            self.summary_label.setText(
                f"Completati: {self.ok_count}  Saltati: {self.skip_count}  Falliti: {self.err_count}"
            )


def main():
    app = QApplication(sys.argv)
    # The native Windows 11 style ignores QSS background-color on some container
    # widgets (QGroupBox in particular); Fusion always honors the stylesheet.
    app.setStyle("Fusion")
    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
