"""ClipMerger - unisce sigla iniziale e finale a un batch di episodi.

Avvio: python main.py
"""
from __future__ import annotations

import dataclasses
import json
import re
import sys
import threading
import time
import urllib.request
from collections import Counter
from pathlib import Path

from PySide6.QtCore import Qt, QObject, Signal, QRunnable, QThreadPool, QThread, QSettings, QUrl, QTimer, QSize
from PySide6.QtGui import QDesktopServices, QDragEnterEvent, QDropEvent, QGuiApplication
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QGridLayout,
    QGroupBox, QLabel, QLineEdit, QPushButton, QFileDialog, QTableWidget,
    QTableWidgetItem, QProgressBar, QComboBox, QRadioButton, QButtonGroup,
    QStackedWidget, QSpinBox, QMessageBox, QCheckBox, QHeaderView, QAbstractItemView,
    QToolButton, QMenu, QTabWidget, QDialog, QListWidget, QListWidgetItem, QScrollArea, QFrame,
)

import applog
import coda
import dropwidgets
import encoding
import utils
import merger
from merger import MergeSettings, MergeResult
from tab_dividi import DividiTab
from tab_sostituisci import SostituisciTab
from tab_tracce import TracceTab
from fine_coda import CountdownDialog
from dropwidgets import PathDropEdit
from encoding_panel import EncodingPanel
import style
from coda import QueueManager
from libreria import Library
from style import PageHeader
from tab_coda import QueueBar, QueuePage
from tab_libreria import LibraryPage

APP_TITLE = "ClipMerger"
AT_END_SECONDS = 10  # countdown before closing the program when the queue is done
APP_VERSION = "0.12.1"
GITHUB_LATEST_RELEASE_API = "https://api.github.com/repos/Vipp0/ClipMerger/releases/latest"
GITHUB_RELEASES_PAGE = "https://github.com/Vipp0/ClipMerger/releases/latest"

def version_tuple(version: str) -> tuple[int, ...]:
    """"v0.2.10" -> (0, 2, 10), so versions compare numerically (0.2.10 is newer than 0.2.9)."""
    return tuple(int(n) for n in re.findall(r"\d+", version.split("-")[0]))


CODEC_LABELS = [("H.264", "h264"), ("H.265 (HEVC)", "h265"), ("AV1", "av1")]

# Theme cycle button: order, monochrome (non-emoji) glyph and tooltip per choice.
THEME_CYCLE = ["auto", "light", "dark"]
THEME_ICONS = {"light": "sole", "dark": "luna", "auto": "auto"}
THEME_TOOLTIPS = {
    "light": "Tema: Chiaro (clic per cambiare)",
    "dark": "Tema: Scuro (clic per cambiare)",
    "auto": "Tema: Automatico, segue il sistema (clic per cambiare)",
}

COL_NAME, COL_STATUS, COL_PROGRESS = range(3)

class DropTableWidget(QTableWidget):
    """Video queue table; accepts a dropped folder or a set of video files."""

    folder_dropped = Signal(str)
    files_dropped = Signal(list)
    delete_requested = Signal()

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
        self.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.setToolTip(
            "Seleziona più file con Ctrl o Maiusc (Ctrl+A per tutti), "
            "poi premi Canc o tasto destro per rimuoverli dalla coda."
        )

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Delete:
            self.delete_requested.emit()
            return
        super().keyPressEvent(event)

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


class HwDetectWorker(QThread):
    done = Signal(dict, dict)

    def __init__(self, ffmpeg_path: str):
        super().__init__()
        self.ffmpeg_path = ffmpeg_path

    def run(self):
        result = {}
        reasons = {}
        for _, codec in CODEC_LABELS:
            encoder, reason = utils.detect_hw_encoder_verbose(self.ffmpeg_path, codec)
            result[codec] = encoder
            reasons[codec] = reason
        self.done.emit(result, reasons)


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


def format_kbps(kbps: int) -> str:
    return f"{kbps:,}".replace(",", ".")


def describe_source_codecs(infos: list[tuple[str, int | None]]) -> str:
    """"AV1, ~1.000 kbit/s" for a uniform set; "20 × AV1, 5 × H.264 (da ... a ...)" for a mixed one."""
    counts = Counter(merger.codec_display_name(c) for c, _ in infos)
    rates = [b // 1000 for _, b in infos if b]
    if len(counts) == 1:
        text = next(iter(counts))
        if len(infos) > 1:
            text = f"tutti {text}"
    else:
        text = ", ".join(f"{n} × {name}" for name, n in counts.most_common())
    if rates:
        lo, hi = min(rates), max(rates)
        text += f", ~{format_kbps(lo)} kbit/s" if hi < lo * 1.1 else f", da {format_kbps(lo)} a {format_kbps(hi)} kbit/s"
    return text


class SourceScanWorker(QThread):
    """Reads the codec/bitrate of every queued episode in the background, so the codec
    choice can show what the originals are as soon as the files are loaded."""

    done = Signal(list)  # [(video codec, video bitrate or None), ...]

    def __init__(self, ffprobe_path: str, episodes: list[Path]):
        super().__init__()
        self.ffprobe_path = ffprobe_path
        self.episodes = episodes

    def run(self):
        infos = []
        for episode in self.episodes:
            if self.isInterruptionRequested():
                return
            try:
                info = merger.probe(self.ffprobe_path, episode)
            except merger.MergeError:
                continue
            infos.append((info.video_codec, info.video_bitrate))
        self.done.emit(infos)


class PreflightWorker(QThread):
    """Probes the sigle and every episode off the GUI thread (so the window stays
    responsive and can show progress), then emits the text of the pre-start summary."""

    progress = Signal(int, int)  # files analyzed so far, total
    done = Signal(str, str)  # summary, warnings

    def __init__(self, ffprobe_path: str, intro: str, outro: str, episodes: list[Path]):
        super().__init__()
        self.ffprobe_path = ffprobe_path
        self.intro, self.outro = intro, outro
        self.episodes = episodes
        self.sources: dict[Path, merger.ProbeResult] = {}  # what each episode turned out to be

    def run(self):
        total = bool(self.intro) + bool(self.outro) + len(self.episodes)
        analyzed = 0
        lines = [f"Episodi in coda: {len(self.episodes)}"]
        warnings = []

        def tick():
            nonlocal analyzed
            analyzed += 1
            self.progress.emit(analyzed, total)

        sigle_parts = []
        for label, path in (("iniziale", self.intro), ("finale", self.outro)):
            if not path:
                continue
            if self.isInterruptionRequested():
                return
            try:
                info = merger.probe(self.ffprobe_path, Path(path))
                sigle_parts.append(f"Sigla {label}: {utils.format_duration(info.duration)}")
            except merger.MergeError as exc:
                warnings.append(f"Impossibile analizzare la sigla {label}: {exc}")
            tick()
        if sigle_parts:
            lines.append("  ·  ".join(sigle_parts))

        resolutions = set()
        durations = []
        for episode in self.episodes:
            if self.isInterruptionRequested():
                return
            try:
                info = merger.probe(self.ffprobe_path, episode)
                self.sources[episode] = info
                resolutions.add((info.width, info.height))
                durations.append(info.duration)
            except merger.MergeError as exc:
                warnings.append(f"{episode.name}: {exc}")
            tick()

        if durations:
            lines.append(
                f"Durata episodi: da {utils.format_duration(min(durations))} "
                f"a {utils.format_duration(max(durations))}"
            )
        if self.sources:
            lines.append("Codec degli originali: " + describe_source_codecs(
                [(i.video_codec, i.video_bitrate) for i in self.sources.values()]))
        if resolutions:
            res_str = ", ".join(f"{w}x{h}" for w, h in sorted(resolutions))
            lines.append(f"Risoluzioni rilevate: {res_str}")
            if len(resolutions) > 1:
                warnings.append(
                    "Gli episodi non hanno tutti la stessa risoluzione "
                    "(verranno comunque adattati singolarmente)."
                )

        self.done.emit("\n".join(lines), "\n".join(warnings))


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(APP_TITLE)

        self.qsettings = QSettings("ClipMerger", "ClipMerger")
        saved_geometry = self.qsettings.value("window_geometry")
        if saved_geometry is not None:
            self.restoreGeometry(saved_geometry)
        else:
            self.resize(1240, 800)
        self.setMinimumSize(900, 560)

        self.ffmpeg_status = utils.check_ffmpeg()
        applog.log.info("ffmpeg: %s | ffprobe: %s", self.ffmpeg_status.ffmpeg_path, self.ffmpeg_status.ffprobe_path)
        self.hw_encoders: dict[str, str | None] = {c: None for _, c in CODEC_LABELS}
        self.hw_reasons: dict[str, str] = {c: "" for _, c in CODEC_LABELS}
        self.rows: list[dict] = []  # {path, output}
        self.analyzing = False  # pre-start file analysis in progress
        self.preflight_worker: PreflightWorker | None = None
        self.retired_workers: list[PreflightWorker] = []  # cancelled but possibly still finishing
        self.scan_worker: SourceScanWorker | None = None
        self.source_infos: list[tuple[str, int | None]] = []  # (codec, bitrate) of the queued originals
        self.episode_sources: dict[Path, merger.ProbeResult] = {}  # from the last pre-start analysis
        self.preflight_paths: list[Path] = []
        self.row_notes: dict[int, list[str]] = {}  # per-row remarks shown next to "Completato"

        self.theme_choice = self.qsettings.value("theme", "auto")
        if self.theme_choice not in ("light", "dark", "auto"):
            self.theme_choice = "auto"

        self.library = Library(lambda: self.ffmpeg_status.ffprobe_path)
        self.queue = QueueManager(self._tool_paths)
        self.job_rows: dict[int, list[int]] = {}  # job id -> the table row of each of its units
        self.lib_sig = None  # what the Unisci tool last took from the library
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

        self.library.changed.connect(self._on_library_changed)
        self.library.load()
        self.queue.load()
        QTimer.singleShot(600, self._offer_resume)
        self.update_thread = UpdateCheckWorker()
        self.update_thread.checked.connect(self._on_update_checked)
        self.update_thread.start()

    # ---------------------------------------------------------------- UI build
    def _build_ui(self):
        central = QWidget()  # first tab ("Unisci sigle"); the window's central widget is set at the end
        root = QVBoxLayout(central)
        root.setSpacing(10)

        # Sorgente / sigle / output
        top_grid = QGridLayout()
        top_grid.setSpacing(8)

        top_grid.addWidget(QLabel("Episodi:"), 0, 0)
        self.folder_edit = PathDropEdit("Nessun file: importali dalla libreria (anche trascinandoli qui)", read_only=True)
        top_grid.addWidget(self.folder_edit, 0, 1)
        browse_folder_btn = QPushButton("Libreria...")
        browse_folder_btn.setObjectName("secondary")
        browse_folder_btn.clicked.connect(self._browse_folder)
        top_grid.addWidget(browse_folder_btn, 0, 2)

        top_grid.addWidget(QLabel("Sigla iniziale:"), 1, 0)
        self.intro_edit = PathDropEdit("Trascina qui il file, oppure sfoglia...", read_only=True)
        top_grid.addWidget(self.intro_edit, 1, 1)
        browse_intro_btn = QPushButton("Sfoglia...")
        browse_intro_btn.setObjectName("secondary")
        browse_intro_btn.clicked.connect(lambda: self._browse_file(self.intro_edit))
        top_grid.addWidget(browse_intro_btn, 1, 2)

        top_grid.addWidget(QLabel("Sigla finale:"), 2, 0)
        self.outro_edit = PathDropEdit("Trascina qui il file, oppure sfoglia...", read_only=True)
        top_grid.addWidget(self.outro_edit, 2, 1)
        browse_outro_btn = QPushButton("Sfoglia...")
        browse_outro_btn.setObjectName("secondary")
        browse_outro_btn.clicked.connect(lambda: self._browse_file(self.outro_edit))
        top_grid.addWidget(browse_outro_btn, 2, 2)

        top_grid.addWidget(QLabel("Cartella output:"), 3, 0)
        self.output_edit = PathDropEdit("<cartella puntate>/output")
        top_grid.addWidget(self.output_edit, 3, 1)
        browse_output_btn = QPushButton("Sfoglia...")
        browse_output_btn.setObjectName("secondary")
        browse_output_btn.clicked.connect(self._browse_output)
        top_grid.addWidget(browse_output_btn, 3, 2)

        top_box = QGroupBox("Sorgente, sigle e output")
        top_box.setLayout(top_grid)
        root.addWidget(top_box)

        # Encoding settings: the same panel as in every tool that re-encodes
        self.enc_panel = EncodingPanel("unisci", self.qsettings)
        root.addWidget(self.enc_panel)

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

        self.queue_btn = QPushButton("Aggiungi alla coda")
        self.queue_btn.setObjectName("secondary")
        self.queue_btn.clicked.connect(self._on_queue_clicked)
        bottom.addWidget(self.queue_btn)
        self.start_btn = QPushButton("Avvia ora")
        bottom.addWidget(self.start_btn)

        root.addLayout(bottom)

        self._build_shell(central)

    def _wire_signals(self):
        self.theme_toggle_btn.clicked.connect(self._cycle_theme)
        QGuiApplication.styleHints().colorSchemeChanged.connect(self._on_system_theme_changed)
        self.table.folder_dropped.connect(self._load_folder)
        self.folder_edit.paths_dropped.connect(self._on_paths_dropped)
        self.table.files_dropped.connect(self.library_import)
        self.intro_edit.paths_dropped.connect(lambda paths: self._on_sigla_dropped(self.intro_edit, paths))
        self.outro_edit.paths_dropped.connect(lambda paths: self._on_sigla_dropped(self.outro_edit, paths))
        self.output_edit.paths_dropped.connect(self._on_output_dropped)
        self.enc_panel.changed.connect(self._refresh_source_info)
        self.table.cellDoubleClicked.connect(self._show_row_detail)
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._show_queue_context_menu)
        self.table.delete_requested.connect(self._remove_selected_rows)
        self.start_btn.clicked.connect(self._on_start_stop_clicked)
        self.reset_btn.clicked.connect(self._reset_all)

    def _resolve_theme(self, choice: str) -> str:
        if choice != "auto":
            return choice
        scheme = QGuiApplication.styleHints().colorScheme()
        return "dark" if scheme == Qt.ColorScheme.Dark else "light"

    def _apply_theme(self, theme: str):
        self.theme = theme
        app = QApplication.instance()
        app.setStyleSheet(style.build_stylesheet(theme))
        self._refresh_icons()

    def _on_theme_choice_changed(self, choice: str):
        self.theme_choice = choice
        self._apply_theme(self._resolve_theme(choice))
        self.qsettings.setValue("theme", choice)
        self._refresh_theme_toggle()

    def _cycle_theme(self):
        next_choice = THEME_CYCLE[(THEME_CYCLE.index(self.theme_choice) + 1) % len(THEME_CYCLE)]
        self._on_theme_choice_changed(next_choice)

    def _refresh_theme_toggle(self):
        colors = style.THEMES[getattr(self, "theme", None) or self._resolve_theme(self.theme_choice)]
        self.theme_toggle_btn.setIcon(style.icon(THEME_ICONS[self.theme_choice], colors["text"], size=18))
        self.theme_toggle_btn.setIconSize(QSize(18, 18))
        self.theme_toggle_btn.setToolTip(THEME_TOOLTIPS[self.theme_choice])

    def _on_system_theme_changed(self, *_args):
        if self.theme_choice == "auto":
            self._apply_theme(self._resolve_theme("auto"))

    def closeEvent(self, event):
        busy_tabs = [tab for tab in (self.sostituisci_tab, self.tracce_tab, self.dividi_tab) if tab.is_busy()]
        if self.queue.running or busy_tabs:
            reply = QMessageBox.question(
                self, "Elaborazione in corso",
                "C'è un'elaborazione in corso: chiudendo il programma verrà interrotta e i file parziali eliminati. "
                "La coda resta salvata: al prossimo avvio potrai riprenderla. Chiudere comunque?",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
            )
            if reply != QMessageBox.Yes:
                event.ignore()
                return
            for tab in busy_tabs:
                tab.shutdown()
            # Let the running tasks stop their ffmpeg and delete their partial files
            # before the process goes away (otherwise those files are left behind).
            self.queue.shutdown()
        self.qsettings.setValue("window_geometry", self.saveGeometry())
        self.library.shutdown()
        for worker in [self.preflight_worker, *self.retired_workers]:
            if worker is not None and worker.isRunning():
                worker.requestInterruption()
                worker.wait(5000)
        super().closeEvent(event)

    # ---------------------------------------------------------------- helpers
    def _on_hw_detected(self, result: dict, reasons: dict):
        self.hw_encoders = result
        self.hw_reasons = reasons
        for panel in (self.enc_panel, self.sostituisci_tab.enc_panel, self.dividi_tab.enc_panel):
            panel.set_hardware(result, reasons)

    def _on_update_checked(self, latest_tag: str):
        if not latest_tag:
            return
        latest = latest_tag.lstrip("vV")
        # Only when the published release is NEWER: a build ahead of the latest release
        # (e.g. one being tested before it is published) must not be told to "update".
        if latest and version_tuple(latest) > version_tuple(APP_VERSION):
            self.update_label.setText(
                f'<a href="{GITHUB_RELEASES_PAGE}">Nuova versione disponibile: {latest_tag}</a>'
            )
            self.update_label.show()

    def _scan_sources(self):
        """Re-read codec/bitrate of the queued originals in the background."""
        if self.scan_worker is not None:
            self.scan_worker.done.disconnect(self._on_sources_scanned)
            self.scan_worker.requestInterruption()
            self.retired_workers.append(self.scan_worker)
            self.scan_worker = None
        self.source_infos = []
        self._refresh_source_info()
        if not self.rows or not self.ffmpeg_status.ffprobe_path:
            return
        self.scan_worker = SourceScanWorker(self.ffmpeg_status.ffprobe_path, [i["path"] for i in self.rows])
        self.scan_worker.done.connect(self._on_sources_scanned)
        self.scan_worker.start()

    def _on_sources_scanned(self, infos: list):
        self.source_infos = infos
        self._refresh_source_info()

    def _codec_advice(self, infos: list[tuple[str, int | None]]) -> list[str]:
        """When the chosen codec is less efficient than the originals' and the bitrate is
        tied to theirs, the picture gets blockier: say so, with a bitrate that compensates."""
        opts = self.enc_panel.options()
        if opts.codec == "source" or opts.quality not in ("cbr", "original"):
            return []
        rates: dict[str, list[int]] = {}
        for src_codec, bitrate in infos:
            if bitrate:
                rates.setdefault(src_codec, []).append(bitrate // 1000)
        out_name = merger.codec_display_name(opts.codec)
        advice = []
        for src_codec, values in rates.items():
            avg = sum(values) // len(values)
            needed = merger.compensated_bitrate_kbps(src_codec, avg, opts.codec)
            if needed is None:
                continue
            if opts.quality == "cbr":
                if opts.kbps >= needed * 0.9:
                    continue
                now = f"a {format_kbps(opts.kbps)} kbit/s"
            else:
                now = "con lo stesso bitrate"
            advice.append(
                f"Attenzione: gli originali sono {merger.codec_display_name(src_codec)} a ~{format_kbps(avg)} kbit/s; "
                f"in {out_name} {now} la qualità scende (più \"quadrettoni\"). Per una qualità simile servono circa "
                f"{format_kbps(needed)} kbit/s (Bitrate fisso), oppure scegli una Qualità, oppure il codec "
                f"\"Come l'originale\"."
            )
        return advice

    def _refresh_source_info(self):
        """The note above the codec choice: what the queued originals are, plus any advice."""
        if not self.source_infos:
            self.enc_panel.set_note("")
            return
        text = "Originali: " + describe_source_codecs(self.source_infos)
        if self.enc_panel.options().codec == "source":
            text += ". Ogni episodio esce nel suo codec."
        advice = self._codec_advice(self.source_infos)
        if advice:
            text += "\n" + "\n".join(advice)
        self.enc_panel.set_note(text)

    def _browse_folder(self):
        self.open_library()

    def _queue_locked(self) -> bool:
        """Replacing the queue under a running batch (or while it's being analyzed) makes
        row indices point at the wrong rows, and used to leave Stop/Reset dead for good."""
        if self.analyzing:
            self.summary_label.setText("Non puoi cambiare l'elenco mentre i file vengono analizzati.")
            return True
        return False

    def _on_paths_dropped(self, paths: list):
        """Something dropped on the episodes field: it goes into the library, which the tool then follows."""
        self.library_import(paths)

    def _on_sigla_dropped(self, edit: QLineEdit, paths: list):
        """A sigla field: a file as it is, or the only video inside a dropped folder."""
        found = dropwidgets.single_file(paths)
        if found is None:
            QMessageBox.information(self, "Quale file?", "Trascina direttamente il file giusto: nella cartella ce ne sono più di uno (o nessuno).")
            return
        edit.setText(str(found))

    def _on_output_dropped(self, paths: list):
        folder = dropwidgets.as_folder(paths)
        if folder is not None:
            self.output_edit.setText(str(folder))

    def _load_folder(self, folder: str):
        self.library_import([Path(folder)])

    def is_busy(self) -> bool:
        return self.analyzing

    def use_library_files(self, files: list[Path]):
        """The tool's queue becomes the library's selected videos."""
        if self._queue_locked():
            return
        self.folder_edit.setText(f"{len(files)} file dalla libreria")
        self._load_files(files)
        if not self.output_edit.text():
            self.output_edit.setText(str(files[0].parent / "output"))

    # ---------------------------------------------------------------- shell: sidebar, pages, library
    def _build_shell(self, unisci_page: QWidget):
        self.library_page = LibraryPage(self)
        self.sostituisci_tab = SostituisciTab(self)
        self.dividi_tab = DividiTab(self)
        self.tracce_tab = TracceTab(self)
        self.queue_page = QueuePage(self)
        self.tool_tabs = {"unisci": self, "sostituisci": self.sostituisci_tab, "dividi": self.dividi_tab, "tracce": self.tracce_tab}
        for widget, title, subtitle in (
                (unisci_page, "Unisci sigle", "Aggiunge una sigla iniziale e/o finale a ogni episodio."),
                (self.sostituisci_tab, "Sostituisci sigla", "Toglie la vecchia sigla dagli episodi e ci mette quella nuova."),
                (self.dividi_tab, "Dividi puntate", "Taglia un file lungo in tanti file, uno per puntata o per sketch."),
                (self.tracce_tab, "Modifica tracce", "Cambia audio e sottotitoli dei video senza ricodificarli.")):
            widget.layout().insertWidget(0, PageHeader(title, subtitle))
        for widget in (self.library_page, unisci_page, self.sostituisci_tab, self.dividi_tab, self.tracce_tab, self.queue_page):
            widget.layout().setContentsMargins(24, 20, 24, 16)
            widget.layout().setSpacing(12)
        pages = [("lib", "Libreria", self.library_page), (None, "Strumenti", None),
                 ("unisci", "Unisci sigle", unisci_page), ("sostituisci", "Sostituisci sigla", self.sostituisci_tab),
                 ("dividi", "Dividi puntate", self.dividi_tab), ("tracce", "Modifica tracce", self.tracce_tab),
                 (None, "", None), ("coda", "Coda", self.queue_page)]

        self.pages = QStackedWidget()
        self.nav = QListWidget()
        self.nav.setObjectName("nav")
        self.nav.setFocusPolicy(Qt.NoFocus)
        self.nav.setIconSize(QSize(20, 20))
        self.nav.setSpacing(1)
        self.row_key: dict[int, str] = {}
        self.page_for_row: dict[int, int] = {}
        for key, text, widget in pages:
            item = QListWidgetItem(text)
            self.nav.addItem(item)
            row = self.nav.row(item)
            if widget is None:  # a heading, not a destination
                item.setFlags(Qt.NoItemFlags)
                font = item.font()
                font.setPointSizeF(font.pointSizeF() * 0.95)
                item.setFont(font)
                continue
            holder = widget
            if key in ("unisci", "sostituisci", "dividi", "tracce"):
                # a tool page is tall: when the window is lower than it, it scrolls instead of squeezing its fields
                holder = QScrollArea()
                holder.setWidgetResizable(True)
                holder.setFrameShape(QFrame.NoFrame)
                holder.setWidget(widget)
            self.pages.addWidget(holder)
            self.row_key[row], self.page_for_row[row] = key, self.pages.indexOf(holder)
        self.key_row = {k: r for r, k in self.row_key.items()}
        for tool in self.tool_tabs.values():  # long choices must not set the minimum width of a page
            for box in tool.findChildren(QComboBox):
                box.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
                box.setMinimumContentsLength(12)
        for tool in self.tool_tabs.values():  # the row numbers add nothing
            tool.table.verticalHeader().hide()
        self.nav_icons = {"lib": "libreria", "unisci": "unisci", "sostituisci": "sostituisci", "dividi": "dividi",
                          "tracce": "tracce", "coda": "coda"}

        side = QWidget()
        side.setObjectName("sidebar")
        side.setFixedWidth(210)
        sl = QVBoxLayout(side)
        sl.setContentsMargins(8, 12, 8, 10)
        sl.setSpacing(8)
        title = QLabel("ClipMerger")
        title.setObjectName("sideTitle")
        sl.addWidget(title)
        version = QLabel(f"versione {APP_VERSION}")
        version.setObjectName("sideVersion")
        sl.addWidget(version)
        sl.addSpacing(6)
        sl.addWidget(self.nav, 1)

        self.update_label = QLabel("")
        self.update_label.setOpenExternalLinks(True)
        self.update_label.setWordWrap(True)
        self.update_label.hide()
        sl.addWidget(self.update_label)
        sl.addWidget(QLabel("Quando la coda è finita:"))
        self.at_end_combo = QComboBox()
        self.at_end_combo.addItem("Non fare niente", "none")
        self.at_end_combo.addItem("Chiudi il programma", "close")
        self.at_end_combo.setToolTip(
            "Vale per tutti gli strumenti, solo per questa sessione (all'avvio torna su «Non fare niente»). "
            "Con «Chiudi il programma», a coda finita parte un conto alla rovescia di 10 secondi in cui si può annullare. "
            "Non scatta se fermi tu l'elaborazione.")
        sl.addWidget(self.at_end_combo)
        row = QHBoxLayout()
        self.theme_toggle_btn = QToolButton()
        self.theme_toggle_btn.setObjectName("themeToggle")
        self._refresh_theme_toggle()
        row.addWidget(self.theme_toggle_btn)
        log_btn = QPushButton("Apri i log")
        log_btn.setObjectName("secondary")
        log_btn.setToolTip(
            "Apre la cartella con il file di log del programma: se qualcosa va storto, "
            "mandami il file «clipmerger.log» (e «crash.log», se c'è) che si trova lì.")
        log_btn.clicked.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(applog.log_dir()))))
        row.addWidget(log_btn, 1)
        sl.addLayout(row)

        self.queue.changed.connect(self._on_queue_changed)
        self.queue.unit_changed.connect(self._dispatch_unit)
        self.queue.job_finished.connect(self._dispatch_job_finished)
        self.queue.queue_finished.connect(lambda ok: self._queue_finished())
        self.nav.currentRowChanged.connect(self._on_nav_row)
        holder = QWidget()
        hl = QHBoxLayout(holder)
        hl.setContentsMargins(0, 0, 0, 0)
        hl.setSpacing(0)
        hl.addWidget(side)
        right = QWidget()
        rl = QVBoxLayout(right)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.setSpacing(0)
        rl.addWidget(self.pages, 1)
        self.queue_bar = QueueBar(self)
        rl.addWidget(self.queue_bar)
        hl.addWidget(right, 1)
        self.setCentralWidget(holder)
        self._refresh_icons()
        self.nav.setCurrentRow(0)

    def _refresh_icons(self):
        """The icons take the colours of the current theme."""
        if not hasattr(self, "nav_icons"):
            return
        colors = style.THEMES[getattr(self, "theme", None) or self._resolve_theme(self.theme_choice)]
        for row, key in self.row_key.items():
            self.nav.item(row).setIcon(style.icon(self.nav_icons[key], colors["muted"], colors["accent_text"]))
        self._refresh_theme_toggle()

    def open_library(self):
        self.nav.setCurrentRow(self.key_row["lib"])

    def open_queue(self):
        self.nav.setCurrentRow(self.key_row["coda"])

    def _on_queue_changed(self):
        n = len(self.queue.active_jobs)
        self.nav.item(self.key_row["coda"]).setText(f"Coda  ({n})" if n else "Coda")

    def _dispatch_unit(self, job, idx: int):
        tool = self.tool_tabs.get(job.tool)
        if tool is not None:
            tool.queue_unit_changed(job, idx)

    def _dispatch_job_finished(self, job):
        tool = self.tool_tabs.get(job.tool)
        if tool is not None:
            tool.queue_job_finished(job)

    def _offer_resume(self):
        """At start-up: the queue of the last session was not finished. Nothing starts without asking."""
        if not self.queue.has_unfinished() or self.queue.running:
            return
        jobs = [j for j in self.queue.jobs if j.pending]
        units = sum(j.count("waiting", "running") for j in jobs)
        reply = QMessageBox.question(
            self, "Coda da riprendere",
            f"L'ultima volta la coda non era finita: {len(jobs)} lavori, {units} elementi ancora da fare.\n\n"
            "Riprenderla adesso? (Quello interrotto riparte dall'inizio del suo file.)",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes)
        if reply == QMessageBox.Yes:
            self.queue.start()
        self.open_queue()

    def open_tool(self, key: str):
        if key in self.key_row:
            self.nav.setCurrentRow(self.key_row[key])

    def library_import(self, paths: list) -> int:
        """The one way in for videos: a folder or files go into the library (selected), and the open tool follows."""
        return self.library.add_paths([Path(p) for p in paths])

    def _on_nav_row(self, row: int):
        if row not in self.page_for_row:
            return
        self.pages.setCurrentIndex(self.page_for_row[row])
        self._sync_tool(self.row_key[row])
        if hasattr(self, "queue_bar"):
            self.queue_bar.refresh()

    def _on_library_changed(self):
        n = len(self.library.items)
        self.nav.item(self.key_row["lib"]).setText(f"Libreria  ({n})" if n else "Libreria")
        self._sync_tool(self.row_key.get(self.nav.currentRow(), ""))

    def _sync_tool(self, key: str):
        """A tool shows the library's selected videos: when it is opened, or the library changes under it."""
        tool = self.tool_tabs.get(key)
        if tool is None or tool.is_busy():
            return
        files = self.library.selected_files()
        sig = tuple(files)
        if not files or getattr(tool, "lib_sig", None) == sig:
            return
        tool.lib_sig = sig
        tool.use_library_files(files)

    def _load_files(self, files: list[Path]):
        if self._queue_locked():
            return
        files = self._auto_assign_sigle(files)
        self.rows = []
        self.table.setRowCount(0)
        for f in files:
            self._add_row(f)
        self._scan_sources()

    def _auto_assign_sigle(self, files: list[Path]) -> list[Path]:
        """Pull out files whose name contains both "sigla" and "iniziale"/"finale"
        and assign them straight to the intro/outro fields, instead of queueing
        them. Matched as separate words rather than one exact phrase, so filenames
        with brackets or other text between them (e.g. "Sigla [1080p] Iniziale.mkv")
        are still recognized."""
        intros, outros, remaining = [], [], []
        for f in files:
            name = f.name.lower()
            if "sigla" in name and "iniziale" in name:
                intros.append(f)
            elif "sigla" in name and "finale" in name:
                outros.append(f)
            else:
                remaining.append(f)
        if intros:
            self.intro_edit.setText(str(intros[0]))
        if outros:
            self.outro_edit.setText(str(outros[0]))
        ignored = [f.name for f in intros[1:] + outros[1:]]
        if ignored:
            self.summary_label.setText("Più sigle trovate: usata la prima, ignorate: " + ", ".join(ignored))
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

    def _browse_file(self, target: QLineEdit):
        path, _ = QFileDialog.getOpenFileName(self, "Seleziona video", "", "Video (*.*)")
        if path:
            target.setText(path)

    def _browse_output(self):
        folder = QFileDialog.getExistingDirectory(self, "Seleziona cartella output")
        if folder:
            self.output_edit.setText(folder)

    def _set_row_status(self, row: int, status: str):
        item = self.table.item(row, COL_STATUS)
        if item is not None:  # a row can vanish under a late signal; never let that kill the slot
            item.setText(status)
        if row < len(self.rows):
            self.rows[row]["last_status"] = status

    def _set_row_progress(self, row: int, pct: float):
        bar: QProgressBar | None = self.table.cellWidget(row, COL_PROGRESS)
        if bar is not None:
            bar.setValue(int(pct * 100))

    def _show_row_detail(self, row: int, _column: int):
        if row >= len(self.rows):
            return
        name = self.rows[row]["path"].name
        status = self.rows[row].get("last_status", "")
        QMessageBox.information(self, name, status or "Nessun dettaglio disponibile.")

    def _selected_rows(self) -> list[int]:
        return sorted({idx.row() for idx in self.table.selectionModel().selectedRows()})

    def _remove_selected_rows(self):
        if self.analyzing:
            return
        rows = [r for r in self._selected_rows() if r < len(self.rows)]
        self.job_rows = {}  # the rows shift: the tables of running jobs can no longer be followed
        for row in reversed(rows):  # bottom-up, so earlier indices stay valid
            self.table.removeRow(row)
            del self.rows[row]
        if rows:
            self._scan_sources()

    def _show_queue_context_menu(self, pos):
        if self.analyzing:
            return
        row = self.table.rowAt(pos.y())
        if row < 0 or row >= len(self.rows):
            return
        if row not in self._selected_rows():  # right-clicking outside the selection acts on that row alone
            self.table.clearSelection()
            self.table.selectRow(row)
        count = len(self._selected_rows())
        menu = QMenu(self)
        remove_action = menu.addAction(
            "Rimuovi dalla coda" if count == 1 else f"Rimuovi {count} elementi dalla coda"
        )
        select_all_action = menu.addAction("Seleziona tutto")
        chosen = menu.exec(self.table.viewport().mapToGlobal(pos))
        if chosen == remove_action:
            self._remove_selected_rows()
        elif chosen == select_all_action:
            self.table.selectAll()

    # ---------------------------------------------------------------- batch run
    def _validate(self) -> str | None:
        if not self.rows:
            return "Seleziona prima una cartella con dei video."
        if not self.intro_edit.text() and not self.outro_edit.text():
            return "Seleziona almeno una sigla (iniziale o finale)."
        if not self.output_edit.text():
            return "Seleziona la cartella di output."
        return None

    def _start_batch(self, start_now: bool = True):
        self._start_now = start_now
        error = self._validate()
        if error:
            QMessageBox.warning(self, "Dati mancanti", error)
            return

        self.retired_workers = [w for w in self.retired_workers if w.isRunning()]
        self.preflight_paths = [item["path"] for item in self.rows]
        total = bool(self.intro_edit.text()) + bool(self.outro_edit.text()) + len(self.preflight_paths)
        self.analyzing = True
        self._set_controls_enabled(False)
        self.summary_label.setText("")
        self.global_progress.setValue(0)
        self.eta_label.setText(f"Analisi dei file: 0/{total}")
        self.preflight_worker = PreflightWorker(
            self.ffmpeg_status.ffprobe_path, self.intro_edit.text(), self.outro_edit.text(),
            self.preflight_paths,
        )
        self.preflight_worker.progress.connect(self._on_preflight_progress)
        self.preflight_worker.done.connect(self._on_preflight_done)
        self.preflight_worker.start()

    def _on_preflight_progress(self, analyzed: int, total: int):
        self.eta_label.setText(f"Analisi dei file: {analyzed}/{total}")
        self.global_progress.setValue(int(analyzed * 100 / total) if total else 0)

    def _end_preflight(self):
        self.analyzing = False
        self.global_progress.setValue(0)
        self.eta_label.setText("")
        self._set_controls_enabled(True)

    def _cancel_preflight(self):
        if self.preflight_worker is not None:
            self.preflight_worker.progress.disconnect(self._on_preflight_progress)
            self.preflight_worker.done.disconnect(self._on_preflight_done)
            self.preflight_worker.requestInterruption()
            self.retired_workers.append(self.preflight_worker)
        self._end_preflight()

    def _on_preflight_done(self, summary: str, warnings: str):
        if not self.analyzing:
            return
        self._end_preflight()
        if [item["path"] for item in self.rows] != self.preflight_paths:
            self.summary_label.setText("L'elenco dei file è cambiato durante l'analisi: riprova.")
            return

        output_dir = Path(self.output_edit.text()).resolve()
        if any(p.resolve().parent == output_dir for p in self.preflight_paths):
            warnings = (warnings + "\n" if warnings else "") + (
                "La cartella di output coincide con quella dei file sorgente: una nuova esecuzione "
                "ricodificherebbe anche i file già prodotti."
            )
        self.episode_sources = self.preflight_worker.sources if self.preflight_worker is not None else {}
        advice = self._codec_advice([(i.video_codec, i.video_bitrate) for i in self.episode_sources.values()])
        if advice:
            warnings = (warnings + "\n" if warnings else "") + "\n".join(advice)
        message = summary + (f"\n\nAvvisi:\n{warnings}" if warnings else "")
        reply = QMessageBox.question(
            self, "Riepilogo prima di avviare", message,
            QMessageBox.Yes | QMessageBox.Cancel, QMessageBox.Yes,
        )
        if reply == QMessageBox.Yes:
            self._launch_batch(self._start_now)

    def _launch_batch(self, start_now: bool = True):
        """The analysis is done and confirmed: the episodes go to the queue as one job."""
        opts = self.enc_panel.options()
        hw = self.enc_panel.hw_encoders
        output_dir = Path(self.output_edit.text())
        try:
            output_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            QMessageBox.critical(self, "Errore output", f"Impossibile creare la cartella output: {exc}")
            return
        intro_path = Path(self.intro_edit.text()) if self.intro_edit.text() else None
        outro_path = Path(self.outro_edit.text()) if self.outro_edit.text() else None

        units = []
        used_names: set[str] = set()
        for row, item in enumerate(self.rows):
            episode_path = item["path"]
            info = self.episode_sources.get(episode_path)
            row_settings, notes = encoding.build_settings(opts, info, hw, self.ffmpeg_status.ffmpeg_path)
            suffix, changed = merger.pick_container(episode_path.suffix, row_settings.codec, opts.container)
            if changed:
                notes.append(f"salvato come {suffix}: il formato {episode_path.suffix} non supporta questo codec")
            # Two episodes sharing a name (e.g. "Puntata 1.mp4" and "Puntata 1.avi") would
            # otherwise write the very same output and temp files.
            stem, n = episode_path.stem, 1
            while f"{stem}{suffix}".lower() in used_names:
                n += 1
                stem = f"{episode_path.stem} ({n})"
            used_names.add(f"{stem}{suffix}".lower())
            if n > 1:
                notes.append(f"nome già usato da un altro episodio: salvato come {stem}{suffix}")
            output_path = output_dir / (stem + suffix)
            item["output"] = output_path
            seconds = info.duration if info else 1.0
            units.append(coda.Unit(episode_path.name, {
                "intro": str(intro_path) if intro_path else "", "episode": str(episode_path),
                "outro": str(outro_path) if outro_path else "", "output": str(output_path),
                "settings": dataclasses.asdict(row_settings), "opts": opts.to_dict(), "pre_notes": notes}, weight=max(seconds, 1.0)))
        sigle = " + ".join(x for x, p in (("sigla iniziale", intro_path), ("sigla finale", outro_path)) if p)
        job = coda.Job("unisci", f"Unisci sigle · {len(units)} episodi", f"{sigle} · {encoding.summary(opts)}", opts.parallel,
                       units, str(output_dir))
        for row in range(len(units)):
            self._set_row_status(row, "In coda")
            self._set_row_progress(row, 0.0)
        self.queue.add_job(job, start_now)
        self.job_rows[job.id] = list(range(len(units)))
        self.summary_label.setText("Aggiunti alla coda: " + str(len(units)) + (" (partono subito)" if start_now and self.queue.running else ""))

    def _on_queue_clicked(self):
        self._start_batch(False)

    def _on_start_stop_clicked(self):
        if self.analyzing:
            self._cancel_preflight()
        else:
            self._start_batch(True)

    def _update_start_stop_button(self):
        self.start_btn.setText("Annulla" if self.analyzing else "Avvia ora")
        self.start_btn.setObjectName("danger" if self.analyzing else "")
        self.start_btn.style().unpolish(self.start_btn)
        self.start_btn.style().polish(self.start_btn)

    def _set_controls_enabled(self, enabled: bool):
        self.reset_btn.setEnabled(enabled)
        self.queue_btn.setEnabled(enabled)
        self.enc_panel.setEnabled(enabled)
        self._update_start_stop_button()

    def _reset_all(self):
        self.lib_sig = None
        self.folder_edit.clear()
        self.output_edit.clear()
        self.intro_edit.clear()
        self.outro_edit.clear()
        self.rows = []
        self.job_rows = {}
        self.table.setRowCount(0)
        self._scan_sources()
        self.global_progress.setValue(0)
        self.summary_label.setText("")
        self.eta_label.setText("")

    # ---------------------------------------------------------------- the queue reports back
    def queue_unit_changed(self, job, idx: int):
        rows = self.job_rows.get(job.id)
        if not rows or idx >= len(rows) or rows[idx] >= self.table.rowCount():
            return
        row, u = rows[idx], job.units[idx]
        self._set_row_progress(row, u.pct)
        self._set_row_status(row, coda.unit_status_text(u))

    def queue_job_finished(self, job):
        if job.id in self.job_rows:
            self.summary_label.setText(f"Completati: {job.count('done')}  Saltati: {job.count('skipped')}  Falliti: {job.count('error')}")

    def _queue_finished(self):
        """The queue ran dry by itself: do what the selector in the sidebar says."""
        if self.at_end_combo.currentData() != "close":
            return
        if self.queue.running or self.queue.has_unfinished() or self.analyzing or self.sostituisci_tab.is_busy() or self.tracce_tab.is_busy() or self.dividi_tab.is_busy():
            return  # something else is still going
        if CountdownDialog(self, AT_END_SECONDS).exec() == QDialog.Accepted:
            self.close()

    def _tool_paths(self):
        status = self.ffmpeg_status
        return (status.ffmpeg_path, status.ffprobe_path) if status.ffmpeg_path and status.ffprobe_path else None


def main():
    applog.setup(APP_VERSION)
    app = QApplication(sys.argv)
    # The native Windows 11 style ignores QSS background-color on some container
    # widgets (QGroupBox in particular); Fusion always honors the stylesheet.
    app.setStyle("Fusion")
    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
