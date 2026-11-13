"""Campi di testo e tabelle in cui si possono trascinare file e cartelle da Esplora file.

Ogni campo accetta sia un file sia una cartella (e più file insieme): sta alla scheda decidere cosa farne,
con le funzioni qui sotto (video_files, single_file, as_folder). Perché un trascinamento venga accettato Qt
vuole che lo accettino sia dragEnterEvent sia dragMoveEvent: un QLineEdit normale rifiuta nel secondo i
file (accetta solo il testo), ed è per questo che i campi che accettavano solo il primo non funzionavano."""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Signal
from PySide6.QtGui import QDragEnterEvent, QDragMoveEvent, QDropEvent
from PySide6.QtWidgets import QAbstractItemView, QLineEdit, QTableWidget

import utils

AUDIO_EXTENSIONS = {".wav", ".mp3", ".flac", ".m4a", ".aac", ".ogg", ".opus", ".mka", ".ac3", ".eac3"}


def dropped_paths(mime) -> list[Path]:
    """The local files/folders carried by a drag (existing ones only)."""
    if not mime.hasUrls():
        return []
    paths = [Path(u.toLocalFile()) for u in mime.urls() if u.isLocalFile()]
    return [p for p in paths if p.exists()]


def video_files(paths: list[Path]) -> list[Path]:
    """Every video in the dropped things: a folder gives the videos directly inside it, a file counts if it is a video."""
    found: list[Path] = []
    for p in paths:
        if p.is_dir():
            found += utils.list_video_files(p)
        elif p.is_file() and p.suffix.lower() in utils.VIDEO_EXTENSIONS and not p.name.startswith("."):
            found.append(p)
    seen, unique = set(), []
    for p in found:
        if p not in seen:
            seen.add(p)
            unique.append(p)
    return unique


def single_file(paths: list[Path], audio_too: bool = False) -> Path | None:
    """The one file meant: a dropped file as it is, or the only video (audio too if asked) of a dropped folder.
    None when it is ambiguous (several in the folder) or there is none."""
    if len(paths) != 1:
        return None
    p = paths[0]
    if p.is_file():
        return p
    if p.is_dir():
        wanted = utils.VIDEO_EXTENSIONS | (AUDIO_EXTENSIONS if audio_too else set())
        inside = [f for f in sorted(p.iterdir()) if f.is_file() and f.suffix.lower() in wanted and not f.name.startswith(".")]
        return inside[0] if len(inside) == 1 else None
    return None


def as_folder(paths: list[Path]) -> Path | None:
    """A dropped folder, or the folder of a dropped file."""
    if not paths:
        return None
    p = paths[0]
    return p if p.is_dir() else p.parent


class PathDropEdit(QLineEdit):
    """A line edit that takes dropped files and folders: it only reports them (`paths_dropped`)."""

    paths_dropped = Signal(list)

    def __init__(self, placeholder: str = "", read_only: bool = False):
        super().__init__()
        self.setPlaceholderText(placeholder)
        self.setReadOnly(read_only)
        self.setAcceptDrops(True)

    def dragEnterEvent(self, event: QDragEnterEvent):
        if dropped_paths(event.mimeData()):
            event.acceptProposedAction()

    def dragMoveEvent(self, event: QDragMoveEvent):  # without this a line edit refuses everything but text
        if dropped_paths(event.mimeData()):
            event.acceptProposedAction()

    def dropEvent(self, event: QDropEvent):
        paths = dropped_paths(event.mimeData())
        if paths:
            event.acceptProposedAction()
            self.paths_dropped.emit(paths)


class PathDropTable(QTableWidget):
    """A table in which files and folders can be dropped: it only reports them (`paths_dropped`)."""

    paths_dropped = Signal(list)

    def __init__(self, rows: int, columns: int):
        super().__init__(rows, columns)
        self.setAcceptDrops(True)
        self.viewport().setAcceptDrops(True)
        self.setDragDropMode(QAbstractItemView.DropOnly)

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
