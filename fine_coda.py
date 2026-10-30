"""Finestrella del conto alla rovescia mostrata quando la coda è finita e si è scelto di chiudere il
programma: dà il tempo di cambiare idea."""
from __future__ import annotations

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QDialog, QLabel, QPushButton, QVBoxLayout


class CountdownDialog(QDialog):
    """exec() torna Accepted quando il tempo scade, Rejected se si preme Annulla."""

    def __init__(self, parent, seconds: int):
        super().__init__(parent)
        self.setWindowTitle("Fine della coda")
        self.setModal(True)
        self.remaining = seconds
        layout = QVBoxLayout(self)
        self.label = QLabel()
        layout.addWidget(self.label)
        cancel = QPushButton("Annulla (non chiudere)")
        cancel.setDefault(True)  # un Invio distratto annulla, non chiude
        cancel.clicked.connect(self.reject)
        layout.addWidget(cancel)
        cancel.setFocus()
        self._update_label()
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.timer.start(1000)

    def _update_label(self):
        unit = "secondo" if self.remaining == 1 else "secondi"
        self.label.setText(f"La coda è finita. Il programma si chiude tra {self.remaining} {unit}.")

    def _tick(self):
        self.remaining -= 1
        if self.remaining <= 0:
            self.timer.stop()
            self.accept()
        else:
            self._update_label()

    def showEvent(self, event):
        super().showEvent(event)
        self.raise_()
        self.activateWindow()
