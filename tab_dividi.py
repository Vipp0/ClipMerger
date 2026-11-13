"""Scheda "Dividi puntate": taglia i file che contengono tante puntate (o sketch) in un file per puntata,
col nome «1x012 Titolo». Due tempi: "Analizza" trova i tagli e legge i titoli e li mostra in tabella
(si può correggere tutto), "Avvia" scrive i file nuovi: niente viene scritto prima che l'utente veda la tabella."""
from __future__ import annotations

import json
import re
import tempfile
import threading
from pathlib import Path

from PySide6.QtCore import QObject, QRect, QRunnable, QSize, Qt, QThread, QThreadPool, Signal
from PySide6.QtGui import QBrush, QColor, QIcon, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QDialog, QFileDialog, QGridLayout, QGroupBox, QHBoxLayout, QHeaderView, QLabel,
    QLineEdit, QMessageBox, QProgressBar, QPushButton, QSpinBox, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

import applog
import dividi
import merger
import profili
import profili_ui
import titoli
import dropwidgets
from encoding import EncodingOptions
from encoding_panel import EncodingPanel
from dropwidgets import PathDropEdit, PathDropTable
from profili_ui import ProfileBar, SeriesPickDialog
from tab_sostituisci import fmt_time, parse_time
from utils import list_video_files

PROLOGUES = [("Unito alla prima puntata", "attach"), ("Scartato (la prima puntata parte dal suo stacco)", "drop")]
ENDINGS = [("Pulita: finisce con la scena, senza la transizione", "clean"),
           ("Con la transizione e i titoli di coda (circa 8 secondi)", "credits")]
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
            catalogue = (titoli.cached_season(series_id, season) if series_id else None)
            if catalogue is None:
                catalogue = []
                if series_id and profile.method == "sample" and profile.read_titles:  # the list is needed to name the pieces
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


class SearchWorker(QThread):
    done = Signal(list)
    failed = Signal(str)

    def __init__(self, query: str, key: str):
        super().__init__()
        self.args = (query, key)

    def run(self):
        try:
            self.done.emit(titoli.search_series(*self.args))
        except titoli.TitoliError as exc:
            self.failed.emit(str(exc))
        except Exception as exc:
            applog.log_exception("Dividi puntate (ricerca serie)", exc)
            self.failed.emit(applog.describe_exception(exc))


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
                 opts: EncodingOptions, hw_encoders: dict, title: str, tail: tuple[float, float] | None, deinterlace: bool,
                 bars: tuple[int, int] | None, signals: ProcessSignals, cancel_event: threading.Event):
        super().__init__()
        self.row = row
        self.args = (ffmpeg, ffprobe, src, start, end, output, opts, hw_encoders, title)
        self.tail, self.deinterlace, self.bars = tail, deinterlace, bars
        self.signals, self.cancel_event = signals, cancel_event
        self.setAutoDelete(True)

    def run(self):
        self.signals.status.emit(self.row, "In elaborazione")
        try:
            result = dividi.cut_piece(
                *self.args, progress_cb=lambda pct: self.signals.progress.emit(self.row, pct),
                cancel_event=self.cancel_event, tail=self.tail, deinterlace=self.deinterlace,
                bars=self.bars)
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
        self.file_notes: dict[Path, dividi.FileAnalysis] = {}
        self.catalogue: list[titoli.Episodio] = []
        self.season = 1
        self.state = "idle"  # "idle" | "analyzing" | "processing"
        self.analyze_worker: AnalyzeWorker | None = None
        self.download_worker: DownloadWorker | None = None
        self.search_worker: SearchWorker | None = None
        self.series_id: int | None = None  # the series chosen on TMDB (never shown: the title and details are)
        self.series_label = ""
        self._last_profile_key = "standard"
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
        top.addWidget(QLabel("File:"), 0, 0)
        self.folder_edit = PathDropEdit("Nessun file: importali dalla libreria (anche trascinandoli qui)", read_only=True)
        self.folder_edit.paths_dropped.connect(self._on_paths_dropped)
        top.addWidget(self.folder_edit, 0, 1)
        browse_folder = QPushButton("Libreria...")
        browse_folder.setObjectName("secondary")
        browse_folder.clicked.connect(self._browse_folder)
        top.addWidget(browse_folder, 0, 2)
        browse_files = QPushButton("File...")
        browse_files.hide()  # imports all go through the library
        top.addWidget(browse_files, 0, 3)
        top.addWidget(QLabel("Cartella output:"), 1, 0)
        self.output_edit = PathDropEdit("<cartella dei file>/puntate divise")
        self.output_edit.paths_dropped.connect(self._on_output_dropped)
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
        self.profile_bar = ProfileBar("dividi", dividi.load_profiles, self._new_profile, self._edit_profile,
                                      self._update_profile)
        self.profile_bar.selected.connect(self._profile_changed)
        prof.addWidget(self.profile_bar, 0, 1, 1, 5)
        self.info = QLabel(dividi.STANDARD.description)
        self.info.setWordWrap(True)
        prof.addWidget(self.info, 1, 0, 1, 6)
        prof.addWidget(QLabel("Serie:"), 2, 0)
        self.series_edit = QLineEdit()
        self.series_edit.setPlaceholderText("scrivi il titolo della serie e premi Cerca (facoltativo)")
        self.series_edit.setToolTip(
            "Il titolo della serie, anche in parte: TMDB la cerca per te. Se ce ne sono più di una con quel titolo "
            "ti mostro anno, paese e lingua per scegliere. Serve per i titoli e i numeri delle puntate.")
        self.series_edit.textEdited.connect(self._series_text_edited)
        self.series_edit.returnPressed.connect(self._search_series)
        prof.addWidget(self.series_edit, 2, 1, 1, 3)
        self.search_btn = QPushButton("Cerca")
        self.search_btn.setObjectName("secondary")
        self.search_btn.clicked.connect(self._search_series)
        prof.addWidget(self.search_btn, 2, 4)
        self.series_info = QLabel("")
        prof.addWidget(self.series_info, 2, 5)
        prof.addWidget(QLabel("Stagione:"), 3, 0)
        self.season_spin = QSpinBox()
        self.season_spin.setRange(1, 99)
        self.season_spin.valueChanged.connect(self._refresh_list_status)
        prof.addWidget(self.season_spin, 3, 1)
        prof.addWidget(QLabel("Chiave TMDB:"), 3, 2)
        self.key_edit = QLineEdit()
        self.key_edit.setEchoMode(QLineEdit.Password)
        self.key_edit.setPlaceholderText("serve solo per cercare e scaricare")
        self.key_edit.setToolTip(
            "La tua chiave personale di TMDB (themoviedb.org → Impostazioni → API). Resta su questo PC, "
            "nelle impostazioni del programma. Una volta scaricata la lista di una stagione non serve più.")
        self.key_edit.editingFinished.connect(self._save_settings)
        prof.addWidget(self.key_edit, 3, 3)
        self.list_btn = QPushButton("Scarica la lista")
        self.list_btn.setObjectName("secondary")
        self.list_btn.clicked.connect(self._download_list)
        prof.addWidget(self.list_btn, 3, 4)
        self.list_label = QLabel("")
        prof.addWidget(self.list_label, 3, 5)
        prof.addWidget(QLabel("Nome dei file:"), 4, 0)
        self.style_combo = QComboBox()
        for key, label in titoli.NAME_STYLES:
            self.style_combo.addItem(label, key)
        self.style_combo.setToolTip(
            "Come si chiameranno i file. Il primo è il formato che FileBot, Plex e Jellyfin riconoscono da soli "
            "(Serie - S01E318 - Titolo): se poi li passi da FileBot con TMDB, il nome è già quello giusto.")
        prof.addWidget(self.style_combo, 4, 1, 1, 3)
        prof.addWidget(QLabel("Nome della serie:"), 4, 4)
        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("come lo scrive TMDB")
        self.name_edit.editingFinished.connect(self._save_settings)
        prof.addWidget(self.name_edit, 4, 5)
        prof.setColumnStretch(5, 1)
        prof_box = QGroupBox("Profilo e titoli (TMDB)")
        prof_box.setLayout(prof)
        root.addWidget(prof_box)

        self.enc_panel = EncodingPanel("dividi", self.win.qsettings, EncodingOptions(cap_bitrate=True, parallel=2))
        root.addWidget(self.enc_panel)

        extra = QGridLayout()
        extra.setSpacing(8)
        extra.addWidget(QLabel("Fine di ogni puntata:"), 0, 0)
        self.ending_combo = QComboBox()
        for label, value in ENDINGS:
            self.ending_combo.addItem(label, value)
        self.ending_combo.setToolTip(
            "Se il file originale finisce con una transizione e dei titoli di coda (riconosciuti dal profilo), "
            "«Con la transizione e i titoli di coda» li mette alla fine di ogni puntata, come accade all'ultima.")
        extra.addWidget(self.ending_combo, 0, 1)
        extra.addWidget(QLabel("Inizio senza titolo:"), 1, 0)
        self.prologue_combo = QComboBox()
        for label, value in PROLOGUES:
            self.prologue_combo.addItem(label, value)
        self.prologue_combo.setToolTip(
            "Se ogni file comincia con una breve scenetta senza titolo (il «prologo»), prima del primo stacco, "
            "può restare attaccata alla prima puntata oppure essere scartata.")
        self.prologue_combo.currentIndexChanged.connect(self._apply_prologue_choice)
        extra.addWidget(self.prologue_combo, 1, 1)
        self.bars_check = QCheckBox("Rendi neri i bordi verdi ai lati")
        self.bars_check.setChecked(True)
        self.bars_check.setToolTip(
            "In alcuni pezzi le bande ai lati dell'immagine sono verdi nel file originale invece che nere. "
            "Con questa casella diventano nere, senza toccare l'immagine.")
        extra.addWidget(self.bars_check, 2, 0, 1, 2)
        extra.setColumnStretch(1, 1)
        extra_box = QGroupBox("Tagli e pezzi")
        extra_box.setLayout(extra)
        root.addWidget(extra_box)

        buttons = QHBoxLayout()
        self.analyze_btn = QPushButton("Analizza")
        self.analyze_btn.clicked.connect(self._on_analyze_clicked)
        buttons.addWidget(self.analyze_btn)
        buttons.addStretch(1)
        self.start_btn = QPushButton("Avvia")
        self.start_btn.clicked.connect(self._on_start_clicked)
        buttons.addWidget(self.start_btn)
        root.addLayout(buttons)

        self.table = PathDropTable(0, 10)
        self.table.paths_dropped.connect(self._on_paths_dropped)
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
        try:
            self.series_id = int(qs.value("dividi/serie", 0)) or None
        except (TypeError, ValueError):
            self.series_id = None
        self.series_label = str(qs.value("dividi/serie_etichetta", "")) if self.series_id else ""
        self.series_edit.setText(str(qs.value("dividi/serie_testo", "")) if self.series_id else "")
        self.season_spin.setValue(int(qs.value("dividi/stagione", 1)))
        self.key_edit.setText(str(qs.value("dividi/chiave", "")))
        self.name_edit.setText(str(qs.value("dividi/nome_serie", "")))
        index = self.style_combo.findData(str(qs.value("dividi/stile", "filebot")))
        self.style_combo.setCurrentIndex(max(index, 0))
        self._refresh_list_status()

    def _save_settings(self):
        qs = self.win.qsettings
        qs.setValue("dividi/serie", self.series_id or 0)
        qs.setValue("dividi/serie_testo", self.series_edit.text().strip())
        qs.setValue("dividi/serie_etichetta", self.series_label)
        qs.setValue("dividi/stagione", self.season_spin.value())
        qs.setValue("dividi/chiave", self.key_edit.text().strip())
        qs.setValue("dividi/nome_serie", self.name_edit.text().strip())
        qs.setValue("dividi/stile", self.style_combo.currentData())

    def _profile(self) -> dividi.Profile:
        return self.profile_bar.current() or dividi.STANDARD

    # ------------------------------------------------------------------ profiles: what they remember
    def collect_settings(self) -> dict:
        """The tab as it is now (series, quality, names...), to be saved in a profile."""
        return {"serie": {"id": self.series_id or 0, "nome": self.name_edit.text().strip(),
                          "testo": self.series_edit.text().strip(), "etichetta": self.series_label},
                "codifica": self.enc_panel.options().to_dict(), "fine": self.ending_combo.currentData(),
                "prologo": self.prologue_combo.currentData(), "bordi": self.bars_check.isChecked(),
                "stile": self.style_combo.currentData()}

    def apply_settings(self, d: dict) -> None:
        """Puts a profile's saved settings in the tab (only what the profile has: the rest stays as it is)."""
        if not isinstance(d, dict) or not d:
            return
        serie = d.get("serie")
        if isinstance(serie, dict):
            try:
                self.series_id = int(serie.get("id") or 0) or None
            except (TypeError, ValueError):
                self.series_id = None
            self.series_label = str(serie.get("etichetta", "")) if self.series_id else ""
            self.series_edit.setText(str(serie.get("testo", "")) if self.series_id else "")
            self.name_edit.setText(str(serie.get("nome", "")))
            self._refresh_list_status()
        if isinstance(d.get("codifica"), dict):
            self.enc_panel.set_options(EncodingOptions.from_dict(d["codifica"]))
        elif "qualita" in d:  # a profile saved before the encoding panel was shared by all tools
            self.enc_panel.set_options(self._legacy_options(d))
        for combo, name in ((self.ending_combo, "fine"), (self.prologue_combo, "prologo"), (self.style_combo, "stile")):
            if name in d:
                i = combo.findData(d[name])
                if i >= 0:
                    combo.setCurrentIndex(i)
        if "bordi" in d:
            self.bars_check.setChecked(bool(d["bordi"]))

    @staticmethod
    def _legacy_options(d: dict) -> EncodingOptions:
        level = {14: "max", 16: "high", 18: "good", 20: "balanced", 23: "small"}.get(d.get("qualita"))
        o = EncodingOptions(cap_bitrate=True, parallel=2)
        if level:
            o.quality = level
        elif isinstance(d.get("qualita"), int):
            o.quality, o.crf = "custom", d["qualita"]
        if d.get("preset") in ("fast", "medium", "slow"):
            o.speed = d["preset"]
        if d.get("codec") in ("h265",):
            o.codec = "h265"
        if d.get("deinterlaccia") in ("auto", "always", "never"):
            o.deinterlace = d["deinterlaccia"]
        if isinstance(d.get("paralleli"), int):
            o.parallel = min(max(d["paralleli"], 1), 8)
        return o

    def _profile_changed(self, key: str = ""):
        profile = self._profile()
        self.info.setText(profile.description or ("Un tuo profilo." if not profile.builtin else ""))
        self.apply_settings(profile.settings)
        if key == self._last_profile_key:
            return  # only reloaded (after an edit): what was analysed is still valid
        self._last_profile_key = key
        if self.entries and not self.is_busy():
            self.entries = []
            self.table.setRowCount(0)
            self.info_label.setText("Profilo cambiato: premi Analizza.")
        self._refresh_buttons()

    def _new_profile(self) -> str | None:
        tools = self._tools()
        return profili_ui.edit_dividi(self, None, tools[0], self.collect_settings()) if tools else None

    def _edit_profile(self, key: str) -> bool:
        profile = self.profile_bar.profiles.get(key)
        tools = self._tools()
        return bool(profile and not profile.builtin and tools
                    and profili_ui.edit_dividi(self, profile, tools[0], self.collect_settings()) is not None)

    def _update_profile(self, key: str) -> bool:
        profile = self.profile_bar.profiles.get(key)
        if profile is None or profile.builtin:
            return False
        data = profile.to_dict()
        data["impostazioni"] = self.collect_settings()
        try:
            profili.save("dividi", data, key)
        except profili.ProfiliError as exc:
            QMessageBox.warning(self, "Profilo non aggiornato", str(exc))
            return False
        return True

    # ------------------------------------------------------------------ the series, found by its title
    def _series_text_edited(self, text: str):
        """The title was changed by hand: the series chosen before no longer matches until Cerca confirms it."""
        if self.series_id is not None:
            self.series_id, self.series_label = None, ""
            self._refresh_list_status()

    def _choices(self) -> dict:
        try:
            data = json.loads(str(self.win.qsettings.value("dividi/scelte", "{}")))
            return data if isinstance(data, dict) else {}
        except ValueError:
            return {}

    def _search_series(self):
        if self.is_busy() or self.search_worker is not None:
            return
        text = self.series_edit.text().strip()
        if not text:
            QMessageBox.information(self, "Titolo mancante", "Scrivi il titolo della serie, poi premi Cerca.")
            return
        key = self._key()
        if not key:
            QMessageBox.information(self, "Chiave mancante", "Per cercare la serie serve la tua chiave di TMDB: scrivila nel campo «Chiave TMDB».")
            return
        self._save_settings()
        self.search_btn.setEnabled(False)
        self.series_info.setText("Cerco...")
        worker = SearchWorker(text, key)
        worker.done.connect(lambda results: self._after_search(text, results))
        worker.failed.connect(self._search_failed)
        self.search_worker = worker
        worker.start()

    def _search_failed(self, message: str):
        self.search_worker = None
        self.search_btn.setEnabled(self.state == "idle")
        self.series_info.setText("")
        QMessageBox.warning(self, "Serie non cercata", message)

    def _after_search(self, query: str, results: list):
        self.search_worker = None
        self.search_btn.setEnabled(self.state == "idle")
        self.series_info.setText("")
        if not results:
            QMessageBox.information(self, "Nessuna serie", f"TMDB non conosce nessuna serie che si chiami «{query}». Prova con meno parole.")
            return
        chosen = results[0]
        if len(results) > 1:
            dlg = SeriesPickDialog(self, results, query, preselect=self._choices().get(titoli.normalize(query)))
            if dlg.exec() != QDialog.Accepted or dlg.chosen() is None:
                self._refresh_list_status()
                return
            chosen = dlg.chosen()
        self._pick_series(chosen, query)

    def _pick_series(self, serie: titoli.Serie, query: str):
        self.series_id, self.series_label = serie.id, serie.label()
        self.series_edit.setText(serie.name)
        self.name_edit.setText(serie.name)
        choices = self._choices()
        choices[titoli.normalize(query)] = serie.id
        self.win.qsettings.setValue("dividi/scelte", json.dumps(choices))
        self._save_settings()
        self._refresh_list_status()
        if titoli.cached_season(serie.id, self.season_spin.value()) is None and self._key():
            self._download_list()  # the list of the season is what the titles are checked against

    def _key(self) -> str:
        return self.key_edit.text().strip() or titoli.key_from_file()

    def _refresh_list_status(self):
        self.series_info.setText(f"✔ {self.series_label}" if self.series_id and self.series_label else
                                 ("✔ serie scelta" if self.series_id else ""))
        self.series_info.setToolTip(self.series_label)
        if not self.series_id:
            self.list_label.setText("Nessuna serie scelta")
            return
        cached = titoli.cached_season(self.series_id, self.season_spin.value())
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
        for worker in [self.analyze_worker, self.download_worker, self.search_worker, *self.retired_workers]:
            if worker is not None and worker.isRunning():
                worker.requestInterruption()
                worker.wait(5000)

    def _refresh_buttons(self):
        idle = self.state == "idle"
        self.analyze_btn.setText("Annulla" if self.state == "analyzing" else "Analizza")
        self.analyze_btn.setEnabled(idle or self.state == "analyzing")
        self.start_btn.setText("Stop" if self.state == "processing" else "Avvia")
        self.start_btn.setEnabled(self.state == "processing" or (idle and any(p is not None for _, p, _ in self.entries)))
        for w in (self.folder_edit, self.output_edit, self.profile_bar, self.series_edit, self.search_btn,
                  self.season_spin, self.key_edit, self.list_btn, self.enc_panel, self.ending_combo,
                  self.prologue_combo, self.bars_check, self.style_combo, self.name_edit):
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
        """The tool's files become the library's selected videos."""
        if self.is_busy():
            return
        self._set_files(files, str(files[0].parent))
        self.folder_edit.setText(f"{len(files)} file dalla libreria")

    def _on_output_dropped(self, paths: list):
        folder = dropwidgets.as_folder(paths)
        if folder is not None and not self.is_busy():
            self.output_edit.setText(str(folder))

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
        if not self.series_id:
            QMessageBox.information(self, "Serie mancante", "Scegli prima la serie: scrivi il titolo e premi Cerca.")
            return
        key = self._key()
        if not key:
            QMessageBox.information(self, "Chiave mancante", "Scrivi nel campo la tua chiave di TMDB, poi premi di nuovo.")
            return
        self._save_settings()
        self.list_btn.setEnabled(False)
        self.list_label.setText("Scarico...")
        worker = DownloadWorker(self.series_id, self.season_spin.value(), key)
        worker.done.connect(lambda eps: self._after_download(f"Lista scaricata: {len(eps)} titoli"))
        worker.failed.connect(lambda msg: (self._after_download(""), QMessageBox.warning(self, "Lista non scaricata", msg)))
        self.download_worker = worker
        worker.start()

    def _after_download(self, text: str):
        self.list_btn.setEnabled(self.state == "idle")
        self._refresh_list_status()
        name = titoli.cached_series_name(self.series_id) if self.series_id else ""
        if name and text and not self.name_edit.text().strip():
            self.name_edit.setText(name)
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
        series, season = self.series_id, self.season_spin.value()
        profile = self._profile()
        if profile.method == "sample" and profile.read_titles:  # the titles are read from the picture and checked against TMDB
            if not series:
                QMessageBox.warning(self, "Serie mancante", "Questo profilo legge i titoli dallo schermo: scegli prima la serie "
                                    "(scrivi il titolo nel campo «Serie» e premi Cerca).")
                return
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
        worker = AnalyzeWorker(tools[0], tools[1], list(self.files), profile, series, season, self._key())
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
        self.file_notes = {fa.path: fa for fa in analyses}
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
        self._apply_prologue_choice()
        sure = sum(1 for _, p, _ in self.entries if p is not None and p.sure)
        total = sum(1 for _, p, _ in self.entries if p is not None)
        self.progress.setValue(100)
        n_interlaced = sum(1 for fa in analyses if fa.interlaced)
        self.info_label.setText(
            f"Puntate trovate: {total} — sicure: {sure}, da controllare: {total - sure}"
            + (" (guarda la colonna «Titolo sullo schermo» e correggi)" if total - sure else "")
            + (f" — file interlacciati: {n_interlaced}" if n_interlaced else ""))
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
        fa = self.file_notes.get(path)
        self.table.item(row, COL_FILE).setToolTip(
            path.name + (f"\nInterlacciato: {'sì' if fa.interlaced else 'no'} ({fa.interlace_note})" if fa else ""))
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
            return "Sicura" + (" (bordi verdi)" if piece.bars else "")
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

    def _apply_prologue_choice(self):
        """The first piece of each file starts either with the untitled opening bit or at its own cup."""
        drop = self.prologue_combo.currentData() == "drop"
        self._filling = True
        for row, (_, piece, _) in enumerate(self.entries):
            if piece is None or piece.prologue_start is None:
                continue
            piece.start = piece.cup_cut if drop else piece.prologue_start
            self.table.item(row, COL_START).setText(fmt_time(piece.start))
            self.table.item(row, COL_DUR).setText(fmt_time(piece.duration))
        self._filling = False

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
            stem = titoli.file_stem(self.season, piece.number, piece.title, self.name_edit.text(), self.style_combo.currentData())
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
        opts = self.enc_panel.options()
        applog.log_batch_start(
            "Dividi puntate",
            f"codec {opts.codec}, velocità {opts.speed}, qualità {opts.quality} (CRF {opts.crf}, {opts.kbps} kbit/s), "
            f"fine {self.ending_combo.currentData()}, deinterlaccia {opts.deinterlace}, prologo {self.prologue_combo.currentData()}, "
            f"bordi verdi {'neri' if self.bars_check.isChecked() else 'lasciati'}, cartella di output {output_dir}",
            len(rows), opts.parallel)
        self.cancel_event = threading.Event()
        self.pool.setMaxThreadCount(opts.parallel)
        self.started = set()
        self.job_rows = rows
        self.total_count, self.finished_count = len(rows), 0
        self.ok_count = self.skip_count = self.err_count = 0
        self.progress.setValue(0)
        self.info_label.setText("")
        self.row_pct = {r: 0.0 for r in rows}
        self._set_state("processing")
        mode = opts.deinterlace
        for row in rows:
            path, piece, _ = self.entries[row]
            self.table.item(row, COL_STATUS).setText("In coda")
            self.table.cellWidget(row, COL_PROGRESS).setValue(0)
            self.pool.start(ProcessTask(
                row, tools[0], tools[1], path, piece.start, piece.end, output_dir / (names[row] + path.suffix),
                opts, self.enc_panel.hw_encoders,
                names[row], piece.tail if self.ending_combo.currentData() == "credits" else None,
                mode == "always" or (mode == "auto" and piece.interlaced),
                piece.bars if self.bars_check.isChecked() else None,
                self.signals, self.cancel_event))

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
