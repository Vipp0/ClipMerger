"""La coda in finestra: la pagina «Coda» con tutti i lavori e i loro elementi, e la barra riepilogativa che resta
in fondo alla finestra mentre la coda è chiusa (cosa sta facendo, quanti ne mancano, quanto manca)."""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QTimer, QUrl, Qt
from PySide6.QtGui import QDesktopServices, QFont
from PySide6.QtWidgets import (
    QAbstractItemView, QFrame, QHBoxLayout, QHeaderView, QLabel, QMessageBox, QProgressBar, QPushButton,
    QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

import coda
import utils
from style import PageHeader

COL_JOB, COL_DETAIL, COL_STATE, COL_BAR = range(4)
JOB_STATES = {"waiting": "In attesa", "running": "In corso", "stopped": "Fermato", "done": "Completato"}
UNIT_STATES = {"waiting": "In attesa", "running": "In elaborazione", "done": "Completato", "skipped": "Già presente, saltato",
               "error": "Errore"}


def job_fraction(job: coda.Job) -> float:
    total = sum(u.weight for u in job.units) or 1.0
    return sum(u.weight * (1.0 if u.state in coda.FINISHED else (u.pct if u.state == "running" else 0.0)) for u in job.units) / total


def job_state_text(job: coda.Job) -> str:
    text = JOB_STATES.get(job.state, job.state)
    done = job.count(*coda.FINISHED)
    errors = job.count("error")
    if job.state == "done":
        return text + (f" · {errors} con errori" if errors else "")
    if job.state in ("running", "stopped"):
        return f"{text} · {done} di {len(job.units)}"
    return text


class QueuePage(QWidget):
    def __init__(self, win):
        super().__init__()
        self.win, self.q = win, win.queue
        self.job_items: dict[int, QTreeWidgetItem] = {}
        self.unit_items: dict[tuple[int, int], QTreeWidgetItem] = {}
        self.bars: dict[object, QProgressBar] = {}
        root = QVBoxLayout(self)
        root.setSpacing(10)

        head = PageHeader("Coda", "I lavori in fila e il loro avanzamento.")
        self.clear_btn = QPushButton("Togli i completati")
        self.clear_btn.setObjectName("secondary")
        self.clear_btn.clicked.connect(self.q.clear_finished)
        head.add_action(self.clear_btn)
        self.start_btn = QPushButton("Avvia la coda")
        self.start_btn.clicked.connect(self._start_stop)
        head.add_action(self.start_btn)
        root.addWidget(head)

        self.summary = QLabel("")
        root.addWidget(self.summary)

        self.tree = QTreeWidget()
        self.tree.setColumnCount(4)
        self.tree.setHeaderLabels(["Lavoro", "Dettagli", "Stato", "Avanzamento"])
        self.tree.setRootIsDecorated(True)
        self.tree.setSelectionMode(QAbstractItemView.SingleSelection)
        self.tree.setEditTriggers(QAbstractItemView.NoEditTriggers)
        header = self.tree.header()
        header.setSectionResizeMode(COL_JOB, QHeaderView.Stretch)
        header.setSectionResizeMode(COL_DETAIL, QHeaderView.Stretch)
        header.setSectionResizeMode(COL_STATE, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(COL_BAR, QHeaderView.Fixed)
        self.tree.setColumnWidth(COL_BAR, 170)
        self.tree.itemSelectionChanged.connect(self._update_buttons)
        root.addWidget(self.tree, 1)

        foot = QHBoxLayout()
        self.up_btn, self.down_btn = self._button("Sposta su", lambda: self._move(-1)), self._button("Sposta giù", lambda: self._move(1))
        self.remove_btn = self._button("Togli dalla coda", self._remove)
        self.retry_btn = self._button("Riprova gli errori", self._retry)
        self.open_btn = self._button("Apri la cartella di output", self._open_folder)
        for b in (self.up_btn, self.down_btn, self.remove_btn, self.retry_btn, self.open_btn):
            foot.addWidget(b)
        foot.addStretch(1)
        root.addLayout(foot)

        self.q.changed.connect(self.refresh)
        self.q.unit_changed.connect(self._unit_changed)
        self.timer = QTimer(self)
        self.timer.setInterval(1000)
        self.timer.timeout.connect(self._update_summary)
        self.timer.start()
        self.refresh()

    @staticmethod
    def _button(text, slot) -> QPushButton:
        b = QPushButton(text)
        b.setObjectName("secondary")
        b.clicked.connect(slot)
        return b

    # ------------------------------------------------------------------ the tree
    def refresh(self):
        expanded = {jid for jid, it in self.job_items.items() if it.isExpanded()}
        selected = self._selected_job()
        self.tree.clear()
        self.job_items, self.unit_items, self.bars = {}, {}, {}
        for job in self.q.jobs:
            item = QTreeWidgetItem([job.title, job.detail, job_state_text(job), ""])
            font = QFont(item.font(COL_JOB))
            font.setBold(True)
            item.setFont(COL_JOB, font)
            item.setToolTip(COL_DETAIL, job.detail)
            self.tree.addTopLevelItem(item)
            self.job_items[job.id] = item
            self.tree.setItemWidget(item, COL_BAR, self._bar(job.id, job_fraction(job)))
            for idx, u in enumerate(job.units):
                child = QTreeWidgetItem([u.label, u.note, UNIT_STATES.get(u.state, u.state), ""])
                child.setToolTip(COL_DETAIL, u.note)
                item.addChild(child)
                self.unit_items[(job.id, idx)] = child
                self.tree.setItemWidget(child, COL_BAR, self._bar((job.id, idx), 1.0 if u.state in coda.FINISHED else u.pct))
            item.setExpanded(job.id in expanded or job.state == "running")
        if selected is not None and selected.id in self.job_items:
            self.job_items[selected.id].setSelected(True)
        self._update_buttons()
        self._update_summary()

    def _bar(self, key, fraction: float) -> QProgressBar:
        bar = QProgressBar()
        bar.setRange(0, 100)
        bar.setValue(int(fraction * 100))
        self.bars[key] = bar
        return bar

    def _unit_changed(self, job, idx: int):
        item = self.unit_items.get((job.id, idx))
        job_item = self.job_items.get(job.id)
        if item is None or job_item is None or idx >= len(job.units):
            self.refresh()
            return
        u = job.units[idx]
        item.setText(COL_STATE, UNIT_STATES.get(u.state, u.state))
        item.setText(COL_DETAIL, u.note)
        self.bars[(job.id, idx)].setValue(int((1.0 if u.state in coda.FINISHED else u.pct) * 100))
        job_item.setText(COL_STATE, job_state_text(job))
        self.bars[job.id].setValue(int(job_fraction(job) * 100))
        self._update_summary()

    def _selected_job(self):
        items = self.tree.selectedItems()
        if not items:
            return None
        item = items[0].parent() or items[0]
        return next((j for j in self.q.jobs if self.job_items.get(j.id) is item), None)

    # ------------------------------------------------------------------ buttons
    def _update_buttons(self):
        job = self._selected_job()
        running = self.q.running
        self.start_btn.setText("Ferma" if running else "Avvia la coda")
        self.start_btn.setObjectName("danger" if running else "")
        self.start_btn.style().unpolish(self.start_btn)
        self.start_btn.style().polish(self.start_btn)
        self.start_btn.setEnabled(running or self.q.has_unfinished())
        self.clear_btn.setEnabled(any(j.state == "done" for j in self.q.jobs))
        here = job is not None
        free = here and job.state != "running"
        self.up_btn.setEnabled(free)
        self.down_btn.setEnabled(free)
        self.remove_btn.setEnabled(here)
        self.retry_btn.setEnabled(free and job.count("error") > 0)
        self.open_btn.setEnabled(here and bool(job.output_dir))

    def _start_stop(self):
        if self.q.running:
            self.q.stop()
        else:
            self.q.start()

    def _move(self, delta: int):
        job = self._selected_job()
        if job is not None:
            self.q.move(job.id, delta)

    def _remove(self):
        job = self._selected_job()
        if job is None:
            return
        if job.state == "running" and QMessageBox.question(
                self, "Togli dalla coda", "Questo lavoro è in corso: toglierlo ferma la coda. Continuare?",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No) != QMessageBox.Yes:
            return
        self.q.remove(job.id)

    def _retry(self):
        job = self._selected_job()
        if job is not None:
            self.q.retry(job.id)

    def _open_folder(self):
        job = self._selected_job()
        if job is not None and job.output_dir:
            Path(job.output_dir).mkdir(parents=True, exist_ok=True)
            QDesktopServices.openUrl(QUrl.fromLocalFile(job.output_dir))

    def _update_summary(self):
        s = self.q.summary()
        if not self.q.jobs:
            self.summary.setText("La coda è vuota. Dagli strumenti, «Aggiungi alla coda» o «Avvia ora».")
        elif self.q.running:
            eta = f" · circa {utils.format_duration(s['eta'])} alla fine" if s["eta"] is not None else ""
            self.summary.setText(f"In corso: {s['label']} · {s['left']} elementi da fare{eta}")
        elif self.q.has_unfinished():
            self.summary.setText(f"La coda è ferma: {s['jobs_left']} lavori con {s['left']} elementi da fare. «Avvia la coda» per continuare.")
        else:
            self.summary.setText("Tutto fatto.")
        self._update_buttons()


class QueueBar(QFrame):
    """The strip at the bottom of the window while the queue page is closed."""

    def __init__(self, win):
        super().__init__()
        self.win, self.q = win, win.queue
        self.setObjectName("queueBar")
        row = QHBoxLayout(self)
        row.setContentsMargins(12, 6, 12, 6)
        row.setSpacing(12)
        self.label = QLabel("")
        row.addWidget(self.label, 1)
        self.bar = QProgressBar()
        self.bar.setRange(0, 100)
        self.bar.setFixedWidth(220)
        row.addWidget(self.bar)
        self.eta = QLabel("")
        row.addWidget(self.eta)
        self.toggle = QPushButton("Ferma")
        self.toggle.setObjectName("secondary")
        self.toggle.clicked.connect(self._toggle)
        row.addWidget(self.toggle)
        self.open_btn = QPushButton("Apri la coda ▴")
        self.open_btn.setObjectName("secondary")
        self.open_btn.clicked.connect(win.open_queue)
        row.addWidget(self.open_btn)
        self.q.changed.connect(self.refresh)
        self.q.unit_changed.connect(lambda *_: self.refresh())
        self.timer = QTimer(self)
        self.timer.setInterval(1000)
        self.timer.timeout.connect(self.refresh)
        self.timer.start()
        self.refresh()

    def _toggle(self):
        if self.q.running:
            self.q.stop()
        else:
            self.q.start()

    def refresh(self):
        on_queue_page = self.win.nav.currentRow() == self.win.key_row.get("coda", -2)
        active = self.q.running or self.q.has_unfinished()
        self.setVisible(active and not on_queue_page)
        if not self.isVisible():
            return
        s = self.q.summary()
        self.bar.setValue(int(s["fraction"] * 100) if self.q.running else 0)
        if self.q.running:
            self.label.setText(s["label"])
            eta = f" · circa {utils.format_duration(s['eta'])}" if s["eta"] is not None else ""
            self.eta.setText(f"restano {s['left']} elementi{eta}")
        else:
            self.label.setText(f"Coda ferma · {s['jobs_left']} lavori in attesa")
            self.eta.setText(f"restano {s['left']} elementi")
        self.toggle.setText("Ferma" if self.q.running else "Avvia")
