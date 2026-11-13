"""La pagina «Libreria»: il punto unico in cui si importano i video. Da qui li usano tutti gli strumenti."""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QDragEnterEvent, QDragMoveEvent, QDropEvent
from PySide6.QtWidgets import (
    QAbstractItemView, QFileDialog, QFrame, QHBoxLayout, QHeaderView, QLabel, QMessageBox, QPushButton,
    QTableWidgetItem, QVBoxLayout, QWidget,
)

import libreria
import utils
from style import PageHeader
from dropwidgets import PathDropTable, dropped_paths, video_files

COL_CHECK, COL_NAME, COL_DUR, COL_VIDEO, COL_AUDIO, COL_SUBS, COL_SIZE, COL_STATE = range(8)
TOOL_NAMES = {"unisci": "Unisci sigle", "sostituisci": "Sostituisci sigla", "dividi": "Dividi puntate", "tracce": "Modifica tracce"}


def human_size(n: int) -> str:
    if n >= 1 << 30:
        return f"{n / (1 << 30):.1f} GB".replace(".", ",")
    return f"{n / (1 << 20):.0f} MB"


class DropZone(QLabel):
    """The dashed area at the top: files and folders dropped here are imported."""
    paths_dropped = Signal(list)

    def __init__(self, text: str):
        super().__init__(text)
        self.setObjectName("dropZone")
        self.setAlignment(Qt.AlignCenter)
        self.setMinimumHeight(54)
        self.setAcceptDrops(True)

    def dragEnterEvent(self, event: QDragEnterEvent):
        if dropped_paths(event.mimeData()):
            event.acceptProposedAction()

    def dragMoveEvent(self, event: QDragMoveEvent):
        if dropped_paths(event.mimeData()):
            event.acceptProposedAction()

    def dropEvent(self, event: QDropEvent):
        paths = dropped_paths(event.mimeData())
        if paths:
            event.acceptProposedAction()
            self.paths_dropped.emit(paths)


class LibTable(PathDropTable):
    delete_requested = Signal()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Delete:
            self.delete_requested.emit()
        else:
            super().keyPressEvent(event)


class LibraryPage(QWidget):
    def __init__(self, win):
        super().__init__()
        self.win = win
        self.lib = win.library
        self._filling = False
        root = QVBoxLayout(self)
        root.setSpacing(10)

        head = PageHeader("Libreria", "I video con cui lavorano tutti gli strumenti: importali una volta sola.")
        for text, slot, secondary in (("Importa file...", self._import_files, True), ("Importa cartella...", self._import_folder, False)):
            btn = QPushButton(text)
            if secondary:
                btn.setObjectName("secondary")
            btn.clicked.connect(slot)
            head.add_action(btn)
        root.addWidget(head)

        self.zone = DropZone("Trascina qui cartelle o file video: restano in libreria e tutti gli strumenti li usano")
        self.zone.paths_dropped.connect(self.import_paths)
        root.addWidget(self.zone)

        self.banner = QFrame()
        self.banner.setObjectName("banner")
        self.banner_layout = QVBoxLayout(self.banner)
        self.banner_layout.setContentsMargins(10, 8, 10, 8)
        self.banner_layout.setSpacing(6)
        self.banner.hide()
        root.addWidget(self.banner)

        self.summary = QLabel("")
        root.addWidget(self.summary)

        self.table = LibTable(0, 8)
        self.table.paths_dropped.connect(self.import_paths)
        self.table.delete_requested.connect(self._remove_selected)
        self.table.setHorizontalHeaderLabels(["", "Nome", "Durata", "Video", "Audio", "Sott.", "Dimensione", "Stato"])
        header = self.table.horizontalHeader()
        for col in (COL_CHECK, COL_DUR, COL_SUBS, COL_SIZE):
            header.setSectionResizeMode(col, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(COL_NAME, QHeaderView.Stretch)
        header.setSectionResizeMode(COL_VIDEO, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(COL_AUDIO, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(COL_STATE, QHeaderView.ResizeToContents)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.table.verticalHeader().hide()
        self.table.itemChanged.connect(self._on_item_changed)
        root.addWidget(self.table, 1)

        foot = QHBoxLayout()
        for text, slot in (("Seleziona tutto", lambda: self.lib.select_all(True)), ("Nessuno", lambda: self.lib.select_all(False)),
                           ("Rimuovi le righe selezionate", self._remove_selected), ("Svuota la libreria", self._clear)):
            btn = QPushButton(text)
            btn.setObjectName("secondary")
            btn.clicked.connect(slot)
            foot.addWidget(btn)
        foot.addStretch(1)
        root.addLayout(foot)

        self.lib.changed.connect(self.refresh)
        self.lib.item_read.connect(self._update_row_for)
        self.lib.reading.connect(lambda *_: self._update_summary())
        self.refresh()

    # ------------------------------------------------------------------ importing
    def import_paths(self, paths: list) -> None:
        added = self.win.library_import(paths)
        if added == 0 and not video_files([Path(p) for p in paths]):
            QMessageBox.warning(self, "Nessun video", "Nessun file video trovato.")

    def _import_folder(self):
        folder = QFileDialog.getExistingDirectory(self, "Seleziona una cartella di video")
        if folder:
            self.import_paths([Path(folder)])

    def _import_files(self):
        paths, _ = QFileDialog.getOpenFileNames(self, "Seleziona dei video", "", "Video (*.mkv *.mp4 *.avi *.mov *.ts *.m2ts *.wmv *.flv *.webm *.mpg *.mpeg)")
        if paths:
            self.import_paths([Path(p) for p in paths])

    def _remove_selected(self):
        rows = sorted({i.row() for i in self.table.selectedIndexes()})
        paths = [self.lib.items[r].path for r in rows if r < len(self.lib.items)]
        if paths:
            self.lib.remove(paths)

    def _clear(self):
        if self.lib.items and QMessageBox.question(
                self, "Svuota la libreria", "Togliere tutti i video dalla libreria? I file non vengono toccati.",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No) == QMessageBox.Yes:
            self.lib.clear()

    # ------------------------------------------------------------------ showing it
    def refresh(self):
        items = self.lib.items
        same = self.table.rowCount() == len(items) and all(
            self.table.item(r, COL_NAME) is not None and self.table.item(r, COL_NAME).data(Qt.UserRole) == str(i.path)
            for r, i in enumerate(items))
        self._filling = True
        if not same:
            self.table.setRowCount(len(items))
            for r in range(len(items)):
                self._fill_row(r)
        for r, i in enumerate(items):
            self.table.item(r, COL_CHECK).setCheckState(Qt.Checked if i.selected else Qt.Unchecked)
        self._filling = False
        self._update_summary()
        self._update_banner()

    def _fill_row(self, r: int):
        item = self.lib.items[r]
        check = QTableWidgetItem()
        check.setFlags(Qt.ItemIsEnabled | Qt.ItemIsUserCheckable | Qt.ItemIsSelectable)
        self.table.setItem(r, COL_CHECK, check)
        name = QTableWidgetItem(item.name)
        name.setData(Qt.UserRole, str(item.path))
        name.setToolTip(str(item.path))
        self.table.setItem(r, COL_NAME, name)
        for col in (COL_DUR, COL_VIDEO, COL_AUDIO, COL_SUBS, COL_SIZE, COL_STATE):
            self.table.setItem(r, col, QTableWidgetItem(""))
        self._fill_info(r)

    def _fill_info(self, r: int):
        item = self.lib.items[r]
        info = item.info
        values = {COL_DUR: "", COL_VIDEO: "", COL_AUDIO: "", COL_SUBS: "", COL_SIZE: "", COL_STATE: ""}
        if info:
            values.update({COL_DUR: utils.format_duration(info["duration"]), COL_VIDEO: libreria.describe_video(info),
                           COL_AUDIO: libreria.describe_audio(info), COL_SUBS: str(info["subs"]) if info["subs"] else "-",
                           COL_SIZE: human_size(info["size"])})
        if item.missing:
            values[COL_STATE] = "Non trovato"
        elif item.error:
            values[COL_STATE] = "Non leggibile"
        elif not info:
            values[COL_STATE] = "Lettura..."
        else:
            values[COL_STATE] = "Pronto"
        for col, text in values.items():
            cell = self.table.item(r, col)
            if cell is not None:
                cell.setText(text)
                cell.setToolTip(item.error if (col == COL_STATE and item.error) else "")

    def _update_row_for(self, path: str):
        for r, i in enumerate(self.lib.items):
            if str(i.path) == path:
                self._filling = True
                self._fill_info(r)
                self._filling = False
                break
        self._update_summary()
        self._update_banner()

    def _update_summary(self):
        items = self.lib.items
        if not items:
            self.summary.setText("Nessun video: importa una cartella o dei file.")
            return
        chosen = [i for i in items if i.selected]
        total = sum(i.info["duration"] for i in chosen if i.info)
        size = sum(i.info["size"] for i in chosen if i.info)
        text = f"{len(items)} video · {len(chosen)} selezionati"
        if total:
            text += f" · {utils.format_duration(total)} in tutto · {human_size(size)}"
        pending = sum(1 for i in items if i.info is None and not i.error and not i.missing)
        if pending:
            text += f" · lettura dei file: ne mancano {pending}"
        missing = sum(1 for i in items if i.missing)
        if missing:
            text += f" · {missing} non trovati (spostati o cancellati)"
        self.summary.setText(text)

    def _update_banner(self):
        while self.banner_layout.count():
            w = self.banner_layout.takeAt(0)
            if w.widget() is not None:
                w.widget().deleteLater()
            elif w.layout() is not None:
                while w.layout().count():
                    sub = w.layout().takeAt(0)
                    if sub.widget() is not None:
                        sub.widget().deleteLater()
        found = libreria.suggestions(self.lib.items)
        self.banner.setVisible(bool(found))
        for s in found:
            row = QHBoxLayout()
            label = QLabel(s.text)
            label.setWordWrap(True)
            row.addWidget(label, 1)
            for key in s.tools:
                btn = QPushButton(f"Apri {TOOL_NAMES[key]}")
                btn.setObjectName("secondary")
                btn.clicked.connect(lambda _=False, k=key: self.win.open_tool(k))
                row.addWidget(btn)
            holder = QWidget()
            holder.setLayout(row)
            row.setContentsMargins(0, 0, 0, 0)
            self.banner_layout.addWidget(holder)

    def _on_item_changed(self, cell: QTableWidgetItem):
        if self._filling or cell.column() != COL_CHECK:
            return
        r = cell.row()
        if r < len(self.lib.items):
            self.lib.set_selected(self.lib.items[r].path, cell.checkState() == Qt.Checked)
