"""Interfaccia dei profili: la barra col menù dei profili (nuovo, modifica, duplica, elimina, esporta, importa),
le finestre per crearne e modificarne uno in «Sostituisci sigla» e «Dividi puntate», e la scelta della serie
quando la ricerca su TMDB ne trova più di una."""
from __future__ import annotations

import subprocess
from pathlib import Path

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QFileDialog,
    QFormLayout, QGroupBox, QHBoxLayout, QHeaderView, QInputDialog, QLabel, QLineEdit, QMenu, QMessageBox,
    QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

import applog
import dividi
import dropwidgets
import profili
import titoli
from dropwidgets import PathDropEdit
from utils import CREATE_NO_WINDOW


def _parse_time(text: str) -> float | None:
    from tab_sostituisci import parse_time  # imported here: that module imports this one
    return parse_time(text)


# ---------------------------------------------------------------------------- the bar with the profile menu
class ProfileBar(QWidget):
    """«Profilo: [menù a tendina] [Gestisci ▾]». It knows nothing about what a profile contains: the tab gives it
    how to list the profiles and how to create, edit and update one."""

    selected = Signal(str)  # the key of the profile chosen (also after the list is reloaded)

    def __init__(self, kind: str, loader, new_cb, edit_cb, update_cb, noun: str = "profilo", plural: str = "profili"):
        super().__init__()
        self.kind, self.loader, self.new_cb, self.edit_cb, self.update_cb = kind, loader, new_cb, edit_cb, update_cb
        self.noun = noun
        self.profiles: dict = {}
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.combo = QComboBox()
        self.combo.setMinimumWidth(260)
        self.combo.currentIndexChanged.connect(lambda _: self.selected.emit(self.current_key()))
        layout.addWidget(self.combo, 1)
        self.button = QPushButton(f"Gestisci {plural} ▾")
        self.button.setObjectName("secondary")
        self.menu = QMenu(self)
        self.act_new = self.menu.addAction(f"Nuovo {noun}...", self._new)
        self.act_edit = self.menu.addAction(f"Modifica questo {noun}...", self._edit)
        self.act_update = self.menu.addAction(f"Salva le impostazioni attuali nel {noun}", self._update)
        self.menu.addSeparator()
        self.act_dup = self.menu.addAction("Duplica...", self._duplicate)
        self.act_del = self.menu.addAction("Elimina...", self._delete)
        self.menu.addSeparator()
        self.act_export = self.menu.addAction("Esporta in un file...", self._export)
        self.menu.addAction("Importa da un file...", self._import)
        self.menu.aboutToShow.connect(self._refresh_actions)
        self.button.setMenu(self.menu)
        layout.addWidget(self.button)
        self.reload()

    def current_key(self) -> str:
        return self.combo.currentData() or "standard"

    def current(self):
        return self.profiles.get(self.current_key())

    def reload(self, select: str | None = None) -> None:
        wanted = select or (self.combo.currentData() if self.combo.count() else "standard")
        self.profiles = self.loader()
        applog.log.info("Profili %s: %s -> %s", self.kind, profili.folder(self.kind), ", ".join(p.name for p in self.profiles.values()))
        self.combo.blockSignals(True)
        self.combo.clear()
        for key, p in self.profiles.items():
            self.combo.addItem(p.name, key)
        self.combo.setCurrentIndex(max(self.combo.findData(wanted), 0))
        self.combo.blockSignals(False)
        self.selected.emit(self.current_key())

    def _refresh_actions(self) -> None:
        user_made = not (self.current() and self.current().builtin)
        for a in (self.act_edit, self.act_update, self.act_del):
            a.setEnabled(user_made)
        self.act_dup.setEnabled(True)

    def _new(self) -> None:
        key = self.new_cb()
        if key:
            self.reload(key)

    def _edit(self) -> None:
        key = self.current_key()
        if self.edit_cb(key):
            self.reload(key)

    def _update(self) -> None:
        key = self.current_key()
        if self.update_cb(key):
            self.reload(key)
            QMessageBox.information(self, "Aggiornato", f"Le impostazioni attuali sono salvate nel {self.noun} «{self.current().name}».")

    def _duplicate(self) -> None:
        current = self.current()
        name, ok = QInputDialog.getText(self, f"Duplica il {self.noun}", "Nome della copia:", text=f"Copia di {current.name}")
        name = name.strip()
        if not ok or not name:
            return
        if profili.name_taken(self.kind, name) or name.lower() == "standard":
            QMessageBox.warning(self, "Nome già usato", f"Esiste già un {self.noun} con questo nome.")
            return
        try:
            if current.builtin:  # «Standard» is not a file: copy it from what it would contain
                key = profili.save(self.kind, dict(current.to_dict(), nome=name))
            else:
                key = profili.duplicate(self.kind, self.current_key(), name)
        except profili.ProfiliError as exc:
            QMessageBox.warning(self, "Profilo non duplicato", str(exc))
            return
        self.reload(key)

    def _delete(self) -> None:
        current = self.current()
        if current is None or current.builtin:
            return
        if QMessageBox.question(self, f"Elimina il {self.noun}", f"Eliminare il {self.noun} «{current.name}»?\nNon si può annullare.",
                                QMessageBox.Yes | QMessageBox.No, QMessageBox.No) != QMessageBox.Yes:
            return
        try:
            profili.delete(self.kind, self.current_key())
        except profili.ProfiliError as exc:
            QMessageBox.warning(self, "Profilo non eliminato", str(exc))
            return
        self.reload("standard")

    def _export(self) -> None:
        current = self.current()
        if current is None or current.builtin:
            QMessageBox.information(self, "Esporta", f"Quello predefinito è già in ogni copia del programma: esporta uno dei tuoi.")
            return
        dest, _ = QFileDialog.getSaveFileName(self, f"Esporta il {self.noun}", f"{profili.slug(current.name)}.zip", "File ClipMerger (*.zip)")
        if not dest:
            return
        try:
            profili.export_to(self.kind, self.current_key(), Path(dest))
        except profili.ProfiliError as exc:
            QMessageBox.warning(self, "Profilo non esportato", str(exc))

    def _import(self) -> None:
        src, _ = QFileDialog.getOpenFileName(self, f"Importa un {self.noun}", "", "File ClipMerger (*.zip)")
        if not src:
            return
        try:
            key = profili.import_from(self.kind, Path(src))
        except profili.ProfiliError as exc:
            QMessageBox.warning(self, "Profilo non importato", str(exc))
            return
        self.reload(key)

    def setEnabled(self, enabled: bool) -> None:  # noqa: N802 (Qt name)
        super().setEnabled(enabled)


# ---------------------------------------------------------------------------- helpers shared by the dialogs
def _name_check(parent: QWidget, kind: str, name: str, key: str | None) -> bool:
    if not name:
        QMessageBox.warning(parent, "Nome mancante", "Dai un nome al profilo.")
        return False
    if name.lower() == "standard" or profili.name_taken(kind, name, key):
        QMessageBox.warning(parent, "Nome già usato", "Esiste già un profilo con questo nome: scegline un altro.")
        return False
    return True


def _spin(low: float, high: float, value: float, step: float = 0.1, decimals: int = 1, suffix: str = " s") -> QDoubleSpinBox:
    s = QDoubleSpinBox()
    s.setRange(low, high)
    s.setDecimals(decimals)
    s.setSingleStep(step)
    s.setValue(value)
    s.setSuffix(suffix)
    return s


def _buttons(dialog: QDialog) -> QDialogButtonBox:
    box = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
    box.button(QDialogButtonBox.Save).setText("Salva")
    box.button(QDialogButtonBox.Cancel).setText("Annulla")
    box.accepted.connect(dialog.accept)
    box.rejected.connect(dialog.reject)
    return box


# ---------------------------------------------------------------------------- Sostituisci sigla: a profile
class SostituisciProfileDialog(QDialog):
    def __init__(self, parent, profile, with_settings: bool):
        super().__init__(parent)
        self.setWindowTitle("Modifica il profilo" if profile else "Nuovo profilo")
        self.profile = profile
        self.setMinimumWidth(620)
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.name_edit = QLineEdit(profile.name if profile else "")
        self.name_edit.setPlaceholderText("per esempio il nome della serie")
        form.addRow("Nome:", self.name_edit)
        self.desc_edit = QLineEdit(profile.description if profile else "")
        self.desc_edit.setPlaceholderText("una riga per ricordarti a cosa serve (facoltativa)")
        form.addRow("Descrizione:", self.desc_edit)
        layout.addLayout(form)

        box = QGroupBox("Com'è fatta la vecchia sigla")
        v = QVBoxLayout(box)
        self.card_check = QCheckBox("Il titolo dell'episodio compare dentro la vecchia sigla iniziale: tienilo, senza musica, dopo la nuova")
        self.card_check.setChecked(bool(profile and profile.title_card_in_intro))
        v.addWidget(self.card_check)
        row = QHBoxLayout()
        self.lookback_check = QCheckBox("I vecchi titoli di coda cominciano prima della loro musica: cercane l'inizio nell'immagine, fino a")
        self.lookback = _spin(1.0, 30.0, profile.end_lookback if profile and profile.end_lookback else 12.0)
        self.lookback_check.setChecked(bool(profile and profile.end_lookback > 0))
        self.lookback.setEnabled(self.lookback_check.isChecked())
        self.lookback_check.toggled.connect(self.lookback.setEnabled)
        row.addWidget(self.lookback_check)
        row.addWidget(self.lookback)
        row.addWidget(QLabel("prima"))
        row.addStretch(1)
        v.addLayout(row)
        row = QHBoxLayout()
        row.addWidget(QLabel("Il nero che chiude la vecchia sigla iniziale può cominciare fino a"))
        self.edge = _spin(0.0, 3.0, profile.edge_after if profile else 0.3, 0.1, 1)
        row.addWidget(self.edge)
        row.addWidget(QLabel("dopo la fine della musica"))
        row.addStretch(1)
        v.addLayout(row)
        layout.addWidget(box)

        self.settings_check = QCheckBox(
            "Salva anche le impostazioni attuali della scheda (sigle nuove, qualità, audio...), così il profilo le richiama")
        self.settings_check.setChecked(with_settings)
        layout.addWidget(self.settings_check)
        layout.addWidget(_buttons(self))

    def accept(self) -> None:
        if _name_check(self, "sostituisci", self.name_edit.text().strip(), self.profile.key if self.profile else None):
            super().accept()

    def values(self) -> dict:
        return {"nome": self.name_edit.text().strip(), "descrizione": self.desc_edit.text().strip(),
                "scheda_nella_sigla": self.card_check.isChecked(),
                "indietro_titoli_di_coda": self.lookback.value() if self.lookback_check.isChecked() else 0.0,
                "tolleranza_nero": self.edge.value()}


def edit_sostituisci(parent, profile, current_settings: dict) -> str | None:
    """Opens the dialog for a new profile (profile=None) or for `profile`; saves; returns its key (None if cancelled)."""
    dlg = SostituisciProfileDialog(parent, profile, with_settings=profile is None)
    if dlg.exec() != QDialog.Accepted:
        return None
    data = dlg.values()
    if dlg.settings_check.isChecked():
        data["impostazioni"] = current_settings
    elif profile is not None:
        data["impostazioni"] = profile.settings
    try:
        return profili.save("sostituisci", data, profile.key if profile else None)
    except profili.ProfiliError as exc:
        QMessageBox.warning(parent, "Profilo non salvato", str(exc))
        return None


# ---------------------------------------------------------------------------- Dividi puntate: a profile
class DividiProfileDialog(QDialog):
    PREVIEW_FRAMES = 6

    def __init__(self, parent, profile, ffmpeg: str, with_settings: bool):
        super().__init__(parent)
        self.setWindowTitle("Modifica il profilo" if profile else "Nuovo profilo")
        self.profile, self.ffmpeg = profile, ffmpeg
        self.setMinimumWidth(700)
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.name_edit = QLineEdit(profile.name if profile else "")
        self.name_edit.setPlaceholderText("per esempio il nome della serie")
        form.addRow("Nome:", self.name_edit)
        self.desc_edit = QLineEdit(profile.description if profile else "")
        self.desc_edit.setPlaceholderText("una riga per ricordarti a cosa serve (facoltativa)")
        form.addRow("Descrizione:", self.desc_edit)
        self.method_combo = QComboBox()
        self.method_combo.addItem("Taglia sul nero tra un pezzo e l'altro", "black")
        self.method_combo.addItem("Riconosce un filmato che si ripete all'inizio di ogni pezzo", "sample")
        self.method_combo.setCurrentIndex(1 if profile and profile.method == "sample" else 0)
        self.method_combo.currentIndexChanged.connect(self._method_changed)
        form.addRow("Punti di taglio:", self.method_combo)
        layout.addLayout(form)

        self.black_box = QGroupBox("Sul nero")
        f = QFormLayout(self.black_box)
        self.black_min = _spin(0.02, 3.0, profile.black_min if profile else 0.12, 0.01, 2)
        f.addRow("Il nero conta se dura almeno:", self.black_min)
        self.min_piece = _spin(1.0, 3600.0, profile.min_piece if profile else 30.0, 5.0, 0)
        f.addRow("Un pezzo dura almeno:", self.min_piece)
        layout.addWidget(self.black_box)

        self.sample_box = QGroupBox("Filmato che si ripete")
        s = QVBoxLayout(self.sample_box)
        have = profile is not None and profile.sample and (profili.folder("dividi") / profile.sample).is_file()
        s.addWidget(QLabel(
            "Scegli un video dei tuoi e indica dove si vede il filmato che apre ogni pezzo (di solito qualche secondo). "
            + ("Un campione è già salvato: lascia vuoto per tenerlo." if have else "")))
        row = QHBoxLayout()
        self.video_edit = PathDropEdit("Un video con il filmato (anche trascinato)...")
        self.video_edit.paths_dropped.connect(self._video_dropped)
        row.addWidget(self.video_edit, 1)
        browse = QPushButton("Sfoglia...")
        browse.setObjectName("secondary")
        browse.clicked.connect(self._browse_video)
        row.addWidget(browse)
        s.addLayout(row)
        row = QHBoxLayout()
        row.addWidget(QLabel("Il filmato comincia a"))
        self.start_edit = QLineEdit("0:00.0")
        self.start_edit.setMaximumWidth(80)
        row.addWidget(self.start_edit)
        row.addWidget(QLabel("e dura"))
        self.dur_edit = QLineEdit("4.0")
        self.dur_edit.setMaximumWidth(60)
        row.addWidget(self.dur_edit)
        row.addWidget(QLabel("secondi"))
        show = QPushButton("Mostra i fotogrammi")
        show.setObjectName("secondary")
        show.clicked.connect(self._show_frames)
        row.addWidget(show)
        row.addStretch(1)
        s.addLayout(row)
        self.frames_label = QLabel("")
        self.frames_label.setMinimumHeight(10)
        s.addWidget(self.frames_label)
        f = QFormLayout()
        self.lead = _spin(0.0, 5.0, profile.lead if profile else 0.3, 0.05, 2)
        self.lead.setToolTip("Quanto tempo prima del filmato c'è lo stacco netto dell'immagine su cui si taglia.")
        f.addRow("Lo stacco netto precede il filmato di:", self.lead)
        s.addLayout(f)
        self.titles_check = QCheckBox("Il titolo del pezzo compare scritto sullo schermo: leggilo e confrontalo con la lista di TMDB")
        self.titles_check.setChecked(bool(profile and profile.read_titles))
        s.addWidget(self.titles_check)
        row = QHBoxLayout()
        row.addSpacing(24)
        row.addWidget(QLabel("Compare dopo"))
        self.cap_from = _spin(0.0, 60.0, profile.caption_from if profile else 6.0)
        row.addWidget(self.cap_from)
        row.addWidget(QLabel("dallo stacco e lo si cerca per"))
        self.cap_len = _spin(1.0, 60.0, profile.caption_len if profile else 8.0)
        row.addWidget(self.cap_len)
        row.addStretch(1)
        s.addLayout(row)
        self.titles_check.toggled.connect(lambda on: (self.cap_from.setEnabled(on), self.cap_len.setEnabled(on)))
        self.cap_from.setEnabled(self.titles_check.isChecked())
        self.cap_len.setEnabled(self.titles_check.isChecked())
        self.credits_check = QCheckBox("Il file finisce con titoli di coda su fondo arancione (e l'effetto del televisore che si spegne): non farli entrare nell'ultimo pezzo")
        self.credits_check.setChecked(bool(profile and profile.credits))
        s.addWidget(self.credits_check)
        layout.addWidget(self.sample_box)

        self.settings_check = QCheckBox(
            "Salva anche le impostazioni attuali della scheda (serie TMDB, qualità, nome dei file...), così il profilo le richiama")
        self.settings_check.setChecked(with_settings)
        layout.addWidget(self.settings_check)
        layout.addWidget(_buttons(self))
        self._method_changed()

    def _method_changed(self) -> None:
        sample = self.method_combo.currentData() == "sample"
        self.sample_box.setVisible(sample)
        self.black_box.setVisible(not sample)
        self.adjustSize()

    def _video_dropped(self, paths: list) -> None:
        found = dropwidgets.single_file(paths)
        if found is None:
            QMessageBox.information(self, "Quale file?", "Trascina direttamente il file giusto.")
            return
        self.video_edit.setText(str(found))

    def _browse_video(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Seleziona il video", "", "Video (*.mkv *.mp4 *.avi *.mov *.ts *.mpg *.mpeg)")
        if path:
            self.video_edit.setText(path)

    def range(self) -> tuple[float, float] | None:
        start, dur = _parse_time(self.start_edit.text()), _parse_time(self.dur_edit.text())
        if start is None or dur is None or dur <= 0:
            QMessageBox.warning(self, "Tempi non validi", "Scrivi l'inizio (es. 8:35.4) e la durata in secondi (es. 4).")
            return None
        return start, dur

    def _show_frames(self) -> None:
        video, span = self.video_edit.text().strip(), self.range()
        if not video or span is None:
            if not video:
                QMessageBox.information(self, "Video mancante", "Scegli prima il video.")
            return
        start, dur = span
        n = self.PREVIEW_FRAMES
        pictures = []
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            for i in range(n):
                t = start + dur * (i + 0.5) / n
                out = subprocess.run(
                    [self.ffmpeg, "-v", "error", "-nostdin", "-ss", f"{t:.3f}", "-i", video, "-an", "-frames:v", "1",
                     "-vf", "scale=150:-1", "-f", "image2pipe", "-c:v", "png", "-"],
                    capture_output=True, creationflags=CREATE_NO_WINDOW).stdout
                pix = QPixmap()
                if out and pix.loadFromData(out):
                    pictures.append(pix)
        finally:
            QApplication.restoreOverrideCursor()
        if not pictures:
            self.frames_label.setText("Non riesco a leggere il video in quel punto.")
            return
        w = sum(p.width() for p in pictures) + 4 * (len(pictures) - 1)
        strip = QPixmap(w, max(p.height() for p in pictures))
        strip.fill(Qt.black)
        x = 0
        from PySide6.QtGui import QPainter
        painter = QPainter(strip)
        for p in pictures:
            painter.drawPixmap(x, 0, p)
            x += p.width() + 4
        painter.end()
        self.frames_label.setPixmap(strip.scaledToWidth(min(w, 660), Qt.SmoothTransformation))

    def accept(self) -> None:
        if not _name_check(self, "dividi", self.name_edit.text().strip(), self.profile.key if self.profile else None):
            return
        if self.method_combo.currentData() == "sample":
            has_old = self.profile is not None and self.profile.sample and (profili.folder("dividi") / self.profile.sample).is_file()
            if not self.video_edit.text().strip() and not has_old:
                QMessageBox.warning(self, "Video mancante", "Scegli il video col filmato che apre ogni pezzo.")
                return
            if self.video_edit.text().strip() and self.range() is None:
                return
        super().accept()


def edit_dividi(parent, profile, ffmpeg: str, current_settings: dict) -> str | None:
    """Opens the dialog for a new profile (profile=None) or for `profile`; saves (making the sample when a video was
    chosen); returns its key (None if cancelled or failed)."""
    dlg = DividiProfileDialog(parent, profile, ffmpeg, with_settings=profile is None)
    if dlg.exec() != QDialog.Accepted:
        return None
    sample_mode = dlg.method_combo.currentData() == "sample"
    key = profile.key if profile else profili.new_key("dividi", dlg.name_edit.text().strip())
    data = {"nome": dlg.name_edit.text().strip(), "descrizione": dlg.desc_edit.text().strip(),
            "metodo": "sample" if sample_mode else "black",
            "nero_minimo": dlg.black_min.value(), "pezzo_minimo": dlg.min_piece.value(),
            "campione": profile.sample if profile else "",
            "secondi_prima_del_filmato": dlg.lead.value(), "titolo_sullo_schermo": dlg.titles_check.isChecked() and sample_mode,
            "titolo_dopo": dlg.cap_from.value(), "titolo_per": dlg.cap_len.value(),
            "titoli_di_coda_arancioni": dlg.credits_check.isChecked() and sample_mode}
    data["impostazioni"] = current_settings if dlg.settings_check.isChecked() else (profile.settings if profile else {})
    video = dlg.video_edit.text().strip()
    if sample_mode and video:
        span = dlg.range()
        problem = ""
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            n = dividi.make_sample(ffmpeg, Path(video), span[0], span[1], profili.sample_path("dividi", key))
            data["campione"] = profili.sample_path("dividi", key).name
            applog.log.info("Profilo Dividi: campione di %d fotogrammi salvato", n)
        except Exception as exc:
            applog.log_exception("Profilo Dividi (campione)", exc)
            problem = applog.describe_exception(exc)
        finally:
            QApplication.restoreOverrideCursor()
        if problem:
            QMessageBox.warning(parent, "Campione non creato", problem)
            return None
    try:
        return profili.save("dividi", data, key)
    except profili.ProfiliError as exc:
        QMessageBox.warning(parent, "Profilo non salvato", str(exc))
        return None


# ---------------------------------------------------------------------------- choosing among several series
class SeriesPickDialog(QDialog):
    """TMDB found several series with that title: shows what tells them apart (year, country, language, size)."""

    def __init__(self, parent, results: list, query: str, preselect: int | None = None):
        super().__init__(parent)
        self.setWindowTitle("Quale serie?")
        self.results = results
        self.setMinimumSize(900, 420)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(f"TMDB ha trovato più serie per «{query}». Scegli quella giusta (doppio clic o «Scegli»):"))
        self.table = QTableWidget(len(results), 7)
        self.table.setHorizontalHeaderLabels(["Titolo", "Titolo originale", "Anno", "Paese", "Lingua", "Stagioni / episodi", "Trama"])
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setWordWrap(True)
        self.table.verticalHeader().hide()
        for row, s in enumerate(results):
            size = (f"{s.seasons or '?'} / {s.episodes or '?'}")
            for col, text in enumerate((s.name, s.original_name if s.original_name != s.name else "", s.year, s.country_text,
                                        s.language_text, size, s.overview or "(nessuna trama)")):
                item = QTableWidgetItem(text)
                if col == 6:
                    item.setToolTip(s.overview)
                self.table.setItem(row, col, item)
        header = self.table.horizontalHeader()
        for col in range(6):
            header.setSectionResizeMode(col, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(6, QHeaderView.Stretch)
        self.table.resizeRowsToContents()
        for row in range(len(results)):
            self.table.setRowHeight(row, min(self.table.rowHeight(row), 64))
        self.table.cellDoubleClicked.connect(lambda *_: self.accept())
        layout.addWidget(self.table, 1)
        box = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        box.button(QDialogButtonBox.Ok).setText("Scegli")
        box.button(QDialogButtonBox.Cancel).setText("Annulla")
        box.accepted.connect(self.accept)
        box.rejected.connect(self.reject)
        layout.addWidget(box)
        start = next((i for i, s in enumerate(results) if s.id == preselect), 0)
        self.table.selectRow(start)

    def chosen(self):
        rows = self.table.selectionModel().selectedRows()
        return self.results[rows[0].row()] if rows else None
