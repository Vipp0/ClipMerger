"""Scheda "Modifica tracce": ordine, traccia predefinita, nome, lingua e presenza delle tracce audio e dei
sottotitoli dei video elencati nella tabella (quelli di una cartella, oppure la coda di «Unisci sigle»).
Nessuna ricodifica: si riscrive il contenitore, in un file nuovo."""
from __future__ import annotations

import copy
import threading
from pathlib import Path

from PySide6.QtCore import Qt, QObject, QRunnable, QThread, QThreadPool, Signal
from PySide6.QtGui import QDragEnterEvent, QDropEvent
from PySide6.QtWidgets import (
    QAbstractItemView, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QGridLayout, QGroupBox, QHBoxLayout,
    QHeaderView, QLabel, QLineEdit, QMessageBox, QProgressBar, QPushButton, QSpinBox, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget,
)

import applog
import dropwidgets
import merger
import tracce
import utils
from dropwidgets import PathDropEdit, PathDropTable
from tracce import AUDIO, SUB, FileTracks, Rule, Track

COL_CHECK, COL_NAME, COL_A_BEFORE, COL_A_AFTER, COL_S_BEFORE, COL_S_AFTER, COL_STATUS, COL_PROGRESS = range(8)
ALL_LABEL = "(tutte)"
COMMON_LANGUAGES = ["ita", "eng", "jpn", "fra", "deu", "spa", "und"]


class ReadWorker(QThread):
    progress = Signal(int, int)
    done = Signal(list)

    def __init__(self, ffprobe: str, files: list[Path]):
        super().__init__()
        self.ffprobe, self.files = ffprobe, files

    def run(self):
        result = []
        for i, path in enumerate(self.files):
            if self.isInterruptionRequested():
                return
            result.append(tracce.read_tracks(self.ffprobe, path))
            self.progress.emit(i + 1, len(self.files))
        self.done.emit(result)


class TaskSignals(QObject):
    progress = Signal(int, float)
    status = Signal(int, str)
    finished = Signal(int, object)


class TaskRunner(QRunnable):
    def __init__(self, row: int, ffmpeg: str, ffprobe: str, source: FileTracks, plan: FileTracks, output_dir: Path,
                 signals: TaskSignals, cancel_event: threading.Event):
        super().__init__()
        self.row = row
        self.args = (ffmpeg, ffprobe, source, plan, output_dir)
        self.signals, self.cancel_event = signals, cancel_event
        self.setAutoDelete(True)

    def run(self):
        self.signals.status.emit(self.row, "In elaborazione")
        try:
            result = tracce.remux(*self.args, progress_cb=lambda p: self.signals.progress.emit(self.row, p),
                                  cancel_event=self.cancel_event)
        except Exception as exc:
            applog.log_exception("Modifica tracce", exc)
            result = merger.MergeResult(output_path=Path(), success=False, error=applog.describe_exception(exc))
        self.signals.finished.emit(self.row, result)


# ---------------------------------------------------------------------------- manual editor
class TrackPanel(QGroupBox):
    """The tracks of one kind of a single file, editable: order, default, language, name, delete."""

    def __init__(self, title: str, tracks: list[Track], with_forced: bool):
        super().__init__(title)
        self.items = [{"track": copy.deepcopy(t), "delete": False} for t in tracks]
        self.with_forced = with_forced
        layout = QVBoxLayout(self)
        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(["Lingua", "Nome", "Formato", "Predefinita", "Forzata", "Elimina"])
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(1, QHeaderView.Stretch)
        for col in (0, 2, 3, 4, 5):
            header.setSectionResizeMode(col, QHeaderView.ResizeToContents)
        self.table.setColumnHidden(4, not with_forced)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.itemChanged.connect(self._changed)
        layout.addWidget(self.table)
        buttons = QHBoxLayout()
        up = QPushButton("Su")
        up.setObjectName("secondary")
        up.clicked.connect(lambda: self._move(-1))
        down = QPushButton("Giù")
        down.setObjectName("secondary")
        down.clicked.connect(lambda: self._move(1))
        buttons.addWidget(up)
        buttons.addWidget(down)
        buttons.addStretch(1)
        layout.addLayout(buttons)
        self._refresh()

    def _refresh(self, select: int | None = None):
        self.table.blockSignals(True)
        self.table.setRowCount(len(self.items))
        for row, it in enumerate(self.items):
            t = it["track"]
            lang = QTableWidgetItem(t.language)
            name = QTableWidgetItem(t.title)
            codec = QTableWidgetItem(f"{t.codec}" + (f" ({t.channels} can.)" if t.channels else ""))
            codec.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
            for col, value in ((3, t.default), (4, t.forced), (5, it["delete"])):
                cell = QTableWidgetItem()
                cell.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable | Qt.ItemIsUserCheckable)
                cell.setCheckState(Qt.Checked if value else Qt.Unchecked)
                self.table.setItem(row, col, cell)
            self.table.setItem(row, 0, lang)
            self.table.setItem(row, 1, name)
            self.table.setItem(row, 2, codec)
        self.table.blockSignals(False)
        if select is not None and 0 <= select < len(self.items):
            self.table.selectRow(select)

    def _changed(self, cell: QTableWidgetItem):
        row, col = cell.row(), cell.column()
        it = self.items[row]
        t = it["track"]
        if col == 0:
            t.language = tracce.norm_lang(cell.text())
        elif col == 1:
            t.title = cell.text()
        elif col == 3:
            checked = cell.checkState() == Qt.Checked
            for other in self.items:
                other["track"].default = False
            t.default = checked
            self._refresh(row)
        elif col == 4:
            t.forced = cell.checkState() == Qt.Checked
        elif col == 5:
            it["delete"] = cell.checkState() == Qt.Checked

    def _move(self, step: int):
        row = self.table.currentRow()
        new = row + step
        if row < 0 or not 0 <= new < len(self.items):
            return
        self.items[row], self.items[new] = self.items[new], self.items[row]
        self._refresh(new)

    def result(self) -> list[Track]:
        return [it["track"] for it in self.items if not it["delete"]]


class TrackEditDialog(QDialog):
    def __init__(self, parent, plan: FileTracks):
        super().__init__(parent)
        self.setWindowTitle(plan.path.name)
        self.resize(820, 560)
        self.plan = plan
        layout = QVBoxLayout(self)
        hint = QLabel(
            "Cambia ordine (Su/Giù), traccia predefinita, lingua e nome, oppure spunta «Elimina». "
            "Nessuna ricodifica: il risultato sarà un file nuovo. Lingua come codice di tre lettere (ita, eng, jpn...).")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        self.audio_panel = TrackPanel("Tracce audio", plan.audio, with_forced=False)
        self.sub_panel = TrackPanel("Sottotitoli", plan.subs, with_forced=True)
        layout.addWidget(self.audio_panel)
        layout.addWidget(self.sub_panel)
        box = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        box.accepted.connect(self._accept)
        box.rejected.connect(self.reject)
        layout.addWidget(box)

    def _accept(self):
        audio = self.audio_panel.result()
        if self.plan.audio and not audio:
            QMessageBox.warning(self, "Tracce audio", "Il file deve avere almeno una traccia audio.")
            return
        if audio and not any(t.default for t in audio):
            audio[0].default = True
        self.plan.audio, self.plan.subs = audio, self.sub_panel.result()
        self.accept()


# ---------------------------------------------------------------------------- rules
class RuleRow(QWidget):
    removed = Signal(object)

    def __init__(self, languages: list[str]):
        super().__init__()
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        self.kind = QComboBox()
        self.kind.addItem("Audio", AUDIO)
        self.kind.addItem("Sottotitoli", SUB)
        self.action = QComboBox()
        for value, label in tracce.ACTION_LABELS:
            self.action.addItem(label, value)
        self.language = QComboBox()
        self.language.setEditable(True)
        self.language.setMinimumWidth(110)
        self.set_languages(languages)
        self.language.setCurrentText("ita")
        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("nuovo nome (vuoto = nessun nome)")
        self.name_edit.setMinimumWidth(200)
        self.language_label = QLabel("in lingua:")
        remove = QPushButton("✕")
        remove.setObjectName("secondary")
        remove.setToolTip("Togli questa regola")
        remove.setFixedWidth(34)
        remove.clicked.connect(lambda: self.removed.emit(self))
        self.name_label = QLabel("nome:")
        for widget in (QLabel("Su:"), self.kind, self.action, self.language_label, self.language,
                       self.name_label, self.name_edit, remove):
            row.addWidget(widget)
        row.addStretch(1)
        self.action.currentIndexChanged.connect(self._action_changed)
        self._action_changed()

    def set_languages(self, languages: list[str]):
        current = self.language.currentText()
        self.language.clear()
        self.language.addItems([ALL_LABEL, *languages])
        if current:
            self.language.setCurrentText(current)

    def _action_changed(self):
        action = self.action.currentData()
        self.language.setEnabled(action != tracce.NO_DEFAULT)
        if action in tracce.LANGUAGE_IS_LIST:
            self.language_label.setText("lingue da tenere (separate da virgola):")
            self.language.setToolTip("Esempio: ita, eng. Tutte le altre tracce di questo tipo vengono eliminate.")
        elif action in tracce.LANGUAGE_IS_TARGET:
            self.language_label.setText("lingua da assegnare:")
            self.language.setToolTip("Le tracce senza lingua (o «und») prendono questa lingua.")
        else:
            self.language_label.setText("in lingua:")
            self.language.setToolTip("")
        self.name_label.setVisible(action in tracce.ACTIONS_NEEDING_NAME)
        self.name_edit.setVisible(action in tracce.ACTIONS_NEEDING_NAME)

    def rule(self) -> Rule:
        text = self.language.currentText().strip()
        action = self.action.currentData()
        if action in tracce.LANGUAGE_IS_LIST:
            lang = tracce.ANY_LANGUAGE if text in ("", ALL_LABEL) else text  # "ita, eng": split by apply_rules
        else:
            lang = tracce.ANY_LANGUAGE if text in ("", ALL_LABEL) else tracce.norm_lang(text)
        return Rule(self.kind.currentData(), action, lang, self.name_edit.text())


# ---------------------------------------------------------------------------- the tab
class TracceTab(QWidget):
    queue_finished = Signal()

    def __init__(self, main_window):
        super().__init__()
        self.win = main_window
        self.sources: list[FileTracks] = []
        self.plans: list[FileTracks] = []
        self.manual: set[int] = set()
        self.state = "idle"  # "idle" | "reading" | "processing"
        self.read_worker: ReadWorker | None = None
        self.retired_workers: list[QThread] = []
        self.rule_rows: list[RuleRow] = []
        self.pool = QThreadPool()
        self.signals = TaskSignals()
        self.cancel_event = threading.Event()
        self.started: set[int] = set()
        self.job_rows: list[int] = []
        self.row_pct: dict[int, float] = {}
        self.total_count = self.finished_count = self.ok_count = self.skip_count = self.err_count = 0
        self._build_ui()
        self.signals.progress.connect(self._on_progress)
        self.signals.status.connect(self._on_status)
        self.signals.finished.connect(self._on_finished)

    # ------------------------------------------------------------------ UI
    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setSpacing(10)

        banner = QLabel(
            "<b>Qui modifichi le tracce audio e i sottotitoli dei video elencati nella tabella</b> (quelli selezionati "
            "nella libreria). Non si ricodifica niente: il risultato è un file nuovo in una cartella a parte, "
            "gli originali non vengono toccati.")
        banner.setWordWrap(True)
        root.addWidget(banner)

        top = QGridLayout()
        top.setSpacing(8)
        top.addWidget(QLabel("Video:"), 0, 0)
        self.folder_edit = PathDropEdit("Nessun file: importali dalla libreria (anche trascinandoli qui)", read_only=True)
        self.folder_edit.paths_dropped.connect(self._on_paths_dropped)
        top.addWidget(self.folder_edit, 0, 1)
        browse = QPushButton("Libreria...")
        browse.setObjectName("secondary")
        browse.clicked.connect(self._browse_folder)
        top.addWidget(browse, 0, 2)
        self.queue_btn = QPushButton("")  # kept only so the list of locked controls below stays valid
        self.queue_btn.hide()
        top.addWidget(QLabel("Cartella output:"), 1, 0)
        self.output_edit = PathDropEdit("<cartella video>/tracce modificate")
        self.output_edit.paths_dropped.connect(self._on_output_dropped)
        top.addWidget(self.output_edit, 1, 1)
        browse_out = QPushButton("Sfoglia...")
        browse_out.setObjectName("secondary")
        browse_out.clicked.connect(self._browse_output)
        top.addWidget(browse_out, 1, 2)
        top.setColumnStretch(1, 1)
        top_box = QGroupBox("Video da modificare")
        top_box.setLayout(top)
        root.addWidget(top_box)

        self.rules_layout = QVBoxLayout()
        rules_box = QGroupBox("Regole (valgono per tutti i video della tabella; si applicano dall'alto in basso)")
        rules_outer = QVBoxLayout(rules_box)
        rules_outer.addLayout(self.rules_layout)
        rule_buttons = QHBoxLayout()
        self.add_rule_btn = QPushButton("Aggiungi una regola")
        self.add_rule_btn.setObjectName("secondary")
        self.add_rule_btn.clicked.connect(lambda: self._add_rule())
        rule_buttons.addWidget(self.add_rule_btn)
        self.apply_btn = QPushButton("Applica le regole")
        self.apply_btn.clicked.connect(self._apply_rules)
        self.apply_btn.setToolTip("Calcola le colonne «dopo». Cancella le modifiche fatte a mano.")
        rule_buttons.addWidget(self.apply_btn)
        rule_buttons.addStretch(1)
        rule_buttons.addWidget(QLabel("Elaborazioni parallele:"))
        self.parallel_spin = QSpinBox()
        self.parallel_spin.setRange(1, 4)
        self.parallel_spin.setValue(2)
        rule_buttons.addWidget(self.parallel_spin)
        rules_outer.addLayout(rule_buttons)
        root.addWidget(rules_box)

        buttons = QHBoxLayout()
        self.edit_btn = QPushButton("Modifica a mano il video selezionato...")
        self.edit_btn.setObjectName("secondary")
        self.edit_btn.clicked.connect(self._edit_selected)
        buttons.addWidget(self.edit_btn)
        buttons.addStretch(1)
        self.start_btn = QPushButton("Elabora")
        self.start_btn.clicked.connect(self._on_start_clicked)
        buttons.addWidget(self.start_btn)
        root.addLayout(buttons)

        self.table = PathDropTable(0, 8)
        self.table.paths_dropped.connect(self._on_paths_dropped)
        self.table.setHorizontalHeaderLabels(
            ["", "Video", "Audio prima", "Audio dopo", "Sottotitoli prima", "Sottotitoli dopo", "Stato", "Avanzamento"])
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(COL_NAME, QHeaderView.Stretch)
        for col in (COL_CHECK, COL_A_BEFORE, COL_A_AFTER, COL_S_BEFORE, COL_S_AFTER, COL_STATUS):
            header.setSectionResizeMode(col, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(COL_PROGRESS, QHeaderView.Fixed)
        self.table.setColumnWidth(COL_PROGRESS, 140)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setMinimumHeight(150)
        self.table.cellDoubleClicked.connect(lambda row, _col: self._edit_row(row))
        self.table.setToolTip("Una sigla con * è la traccia predefinita, (f) è un sottotitolo forzato. Doppio clic su una riga per modificare a mano.")
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
        self.cancel_event.set()
        self.pool.clear()
        self.pool.waitForDone(20000)
        for worker in [self.read_worker, *self.retired_workers]:
            if worker is not None and worker.isRunning():
                worker.requestInterruption()
                worker.wait(5000)

    def _set_state(self, state: str):
        self.state = state
        self._refresh_buttons()

    def _changed_rows(self) -> list[int]:
        return [i for i, (s, p) in enumerate(zip(self.sources, self.plans)) if self._will_change(s, p)]

    @staticmethod
    def _will_change(source: FileTracks, plan: FileTracks) -> bool:
        return source.error is None and (
            source.signature() != plan.signature() or tracce.target_suffix(source.path.suffix) != source.path.suffix.lower())

    def _refresh_buttons(self):
        idle = self.state == "idle"
        self.start_btn.setText("Stop" if self.state == "processing" else "Elabora")
        self.start_btn.setEnabled(self.state == "processing" or (idle and bool(self.sources)))
        self.apply_btn.setEnabled(idle and bool(self.sources))
        self.edit_btn.setEnabled(idle and bool(self.sources))
        self.add_rule_btn.setEnabled(idle)
        for w in (self.folder_edit, self.output_edit, self.queue_btn, self.parallel_spin):
            w.setEnabled(idle)
        for rr in self.rule_rows:
            rr.setEnabled(idle)
        self.start_btn.setObjectName("danger" if self.state == "processing" else "")
        self.start_btn.style().unpolish(self.start_btn)
        self.start_btn.style().polish(self.start_btn)

    # ------------------------------------------------------------------ loading
    def _browse_folder(self):
        self.win.open_library()

    def _browse_output(self):
        folder = QFileDialog.getExistingDirectory(self, "Seleziona la cartella di output")
        if folder:
            self.output_edit.setText(folder)

    def _on_paths_dropped(self, paths: list):
        """Dropped here, the videos go into the library, which every tool follows."""
        if not self.is_busy():
            self.win.library_import(paths)

    def use_library_files(self, files: list[Path]):
        """The tool's videos become the library's selected videos."""
        if self.is_busy():
            return
        self.folder_edit.setText(f"{len(files)} file dalla libreria")
        self.output_edit.setText(str(files[0].parent / "tracce modificate"))
        self._read_files(list(files))

    def _on_output_dropped(self, paths: list):
        folder = dropwidgets.as_folder(paths)
        if folder is not None and not self.is_busy():
            self.output_edit.setText(str(folder))

    def _read_files(self, files: list[Path]):
        tools = self._tools()
        if tools is None:
            return
        self.sources, self.plans, self.manual = [], [], set()
        self.table.setRowCount(0)
        self.progress.setValue(0)
        self.info_label.setText("Lettura delle tracce...")
        self.read_worker = ReadWorker(tools[1], files)
        self.read_worker.progress.connect(lambda d, t: (self.progress.setValue(int(d * 100 / t)), self.info_label.setText(f"Lettura delle tracce: {d}/{t}")))
        self.read_worker.done.connect(self._on_read_done)
        self.read_worker.start()
        self._set_state("reading")

    def _on_read_done(self, sources: list):
        self.read_worker = None
        self.sources = sources
        self.plans = [copy.deepcopy(s) for s in sources]
        self.table.setRowCount(0)
        for ft in sources:
            self._add_row(ft)
        self._refresh_languages()
        self.progress.setValue(0)
        self.info_label.setText(f"{len(sources)} video. Aggiungi delle regole e premi «Applica le regole», oppure modifica a mano un video.")
        self._set_state("idle")
        if self.rule_rows:
            self._apply_rules()

    def _add_row(self, ft: FileTracks):
        row = self.table.rowCount()
        self.table.insertRow(row)
        check = QTableWidgetItem()
        check.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled)
        check.setCheckState(Qt.Unchecked)
        self.table.setItem(row, COL_CHECK, check)
        for col in (COL_NAME, COL_A_BEFORE, COL_A_AFTER, COL_S_BEFORE, COL_S_AFTER, COL_STATUS):
            cell = QTableWidgetItem("")
            cell.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
            self.table.setItem(row, col, cell)
        bar = QProgressBar()
        bar.setRange(0, 100)
        self.table.setCellWidget(row, COL_PROGRESS, bar)
        self.table.item(row, COL_NAME).setText(ft.path.name)
        self._fill_row(row)

    def _fill_row(self, row: int, status: str | None = None):
        src, plan = self.sources[row], self.plans[row]
        if src.error:
            self.table.item(row, COL_STATUS).setText(f"Errore: {src.error}")
            self.table.item(row, COL_CHECK).setFlags(Qt.NoItemFlags)
            return
        self.table.item(row, COL_A_BEFORE).setText(tracce.describe(src.audio))
        self.table.item(row, COL_A_AFTER).setText(tracce.describe(plan.audio))
        self.table.item(row, COL_S_BEFORE).setText(tracce.describe(src.subs))
        self.table.item(row, COL_S_AFTER).setText(tracce.describe(plan.subs))
        changes = self._will_change(src, plan)
        if status is None:
            status = ("Modificato a mano" if row in self.manual else "Da modificare") if changes else "Nessuna modifica"
        self.table.item(row, COL_STATUS).setText(status)
        check = self.table.item(row, COL_CHECK)
        check.setFlags((Qt.ItemIsUserCheckable | Qt.ItemIsEnabled) if changes else Qt.NoItemFlags)
        check.setCheckState(Qt.Checked if changes else Qt.Unchecked)

    def _refresh_languages(self):
        found = sorted({t.language for s in self.sources for t in (*s.audio, *s.subs) if t.language})
        self.languages = found + [l for l in COMMON_LANGUAGES if l not in found]
        for rr in self.rule_rows:
            rr.set_languages(self.languages)

    # ------------------------------------------------------------------ rules
    def _add_rule(self):
        rr = RuleRow(getattr(self, "languages", COMMON_LANGUAGES))
        rr.removed.connect(self._remove_rule)
        self.rule_rows.append(rr)
        self.rules_layout.addWidget(rr)

    def _remove_rule(self, rr: RuleRow):
        self.rule_rows.remove(rr)
        self.rules_layout.removeWidget(rr)
        rr.deleteLater()

    def _apply_rules(self):
        rules = [rr.rule() for rr in self.rule_rows]
        self.manual = set()
        skipped = 0
        for i, src in enumerate(self.sources):
            if src.error:
                continue
            plan, notes = tracce.apply_rules(src, rules)
            self.plans[i] = plan
            self._fill_row(i, None)
            if notes:
                skipped += 1
                self.table.item(i, COL_STATUS).setText(self.table.item(i, COL_STATUS).text() + f" — {'; '.join(notes)}")
        changed = len(self._changed_rows())
        self.info_label.setText(f"Da modificare: {changed} su {len(self.sources)}." + (f" Regole saltate per prudenza in {skipped}." if skipped else ""))
        self._refresh_buttons()

    # ------------------------------------------------------------------ manual edit
    def _edit_selected(self):
        row = self.table.currentRow()
        if row < 0:
            QMessageBox.information(self, "Modifica", "Seleziona nella tabella il video da modificare.")
            return
        self._edit_row(row)

    def _edit_row(self, row: int):
        if self.is_busy() or row >= len(self.plans) or self.sources[row].error:
            return
        dialog = TrackEditDialog(self, copy.deepcopy(self.plans[row]))
        if dialog.exec() == QDialog.Accepted:
            self.plans[row] = dialog.plan
            if self.plans[row].signature() != self.sources[row].signature():
                self.manual.add(row)
            else:
                self.manual.discard(row)
            self._fill_row(row)
            self.info_label.setText(f"Da modificare: {len(self._changed_rows())} su {len(self.sources)}.")

    # ------------------------------------------------------------------ processing
    def _on_start_clicked(self):
        if self.state == "processing":
            self._cancel_processing()
        else:
            self._start_processing()

    def _start_processing(self):
        rows = [r for r in range(len(self.sources))
                if self.table.item(r, COL_CHECK).checkState() == Qt.Checked and self._will_change(self.sources[r], self.plans[r])]
        if not rows:
            QMessageBox.information(self, "Niente da fare", "Nessun video da modificare: scrivi delle regole e premi «Applica le regole», oppure modifica a mano un video, poi spunta le righe.")
            return
        tools = self._tools()
        if tools is None:
            return
        output_dir = Path(self.output_edit.text() or (self.sources[0].path.parent / "tracce modificate"))
        if any(self.sources[r].path.resolve().parent == output_dir.resolve() for r in rows):
            QMessageBox.warning(self, "Cartella di output", "La cartella di output coincide con quella dei video: sceglierne un'altra.")
            return
        reply = QMessageBox.question(
            self, "Riepilogo prima di avviare",
            f"Video da modificare: {len(rows)}\nCartella di output: {output_dir}\n\n"
            "Si riscrive solo il contenitore (nessuna ricodifica); gli originali non vengono toccati. Procedere?",
            QMessageBox.Yes | QMessageBox.Cancel, QMessageBox.Yes)
        if reply != QMessageBox.Yes:
            return
        applog.log_batch_start("Modifica tracce", f"cartella di output {output_dir}", len(rows), self.parallel_spin.value())
        self.cancel_event = threading.Event()
        self.pool.setMaxThreadCount(self.parallel_spin.value())
        self.started, self.job_rows = set(), rows
        self.total_count, self.finished_count = len(rows), 0
        self.ok_count = self.skip_count = self.err_count = 0
        self.row_pct = {r: 0.0 for r in rows}
        self.progress.setValue(0)
        self.info_label.setText("")
        self._set_state("processing")
        for row in rows:
            self.table.item(row, COL_STATUS).setText("In coda")
            self.table.cellWidget(row, COL_PROGRESS).setValue(0)
            self.pool.start(TaskRunner(row, tools[0], tools[1], self.sources[row], self.plans[row], output_dir,
                                       self.signals, self.cancel_event))

    def _cancel_processing(self):
        self.cancel_event.set()
        self.pool.clear()
        for row in self.job_rows:
            if row not in self.started:
                self.table.item(row, COL_STATUS).setText("Annullato")
                self.row_pct[row] = 1.0
                self.finished_count += 1
        self._update_global()
        self._check_all_done()

    def _on_status(self, row: int, text: str):
        self.started.add(row)
        self.table.item(row, COL_STATUS).setText(text)

    def _on_progress(self, row: int, pct: float):
        bar = self.table.cellWidget(row, COL_PROGRESS)
        if bar is not None:
            bar.setValue(int(pct * 100))
        self.row_pct[row] = pct
        self.table.item(row, COL_STATUS).setText(f"In elaborazione {int(pct * 100)}%")
        self._update_global()

    def _update_global(self):
        if self.total_count:
            self.progress.setValue(int(sum(self.row_pct.values()) * 100 / self.total_count))

    def _on_finished(self, row: int, result):
        applog.log_result("Modifica tracce", self.sources[row].path.name if row < len(self.sources) else f"riga {row}", result)
        self.finished_count += 1
        self.row_pct[row] = 1.0
        bar = self.table.cellWidget(row, COL_PROGRESS)
        if bar is not None:
            bar.setValue(100)
        if result.skipped:
            self.skip_count += 1
            text = "Saltato (già presente)" if not result.note else f"Saltato — {result.note}"
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
            self.info_label.setText(f"Completati: {self.ok_count}  Saltati: {self.skip_count}  Falliti: {self.err_count}")
            if not self.cancel_event.is_set():
                self.queue_finished.emit()
