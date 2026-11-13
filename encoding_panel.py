"""Il pannello «Codifica», lo stesso in tutti gli strumenti che ricodificano. In vista le scelte di ogni giorno
(codec, velocità, contenitore, qualità, audio); sotto «Avanzate» il resto. In cima i preset: si salvano, si
richiamano, si duplicano ed eliminano come i profili."""
from __future__ import annotations

import json
from dataclasses import dataclass

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QGridLayout, QGroupBox, QInputDialog, QLabel, QMessageBox, QSpinBox, QStackedWidget,
    QToolButton, QVBoxLayout, QWidget,
)

import encoding
import profili
from encoding import EncodingOptions
from profili_ui import ProfileBar

KIND = "codifica"


@dataclass(frozen=True, eq=False)
class Preset:
    key: str
    name: str
    options: EncodingOptions
    builtin: bool = False

    def to_dict(self) -> dict:
        return {"nome": self.name, "opzioni": self.options.to_dict()}


def load_presets() -> dict[str, Preset]:
    """The ready-made presets, then the user's own by name."""
    found = {key: Preset(key, name, opts, True) for key, name, opts in encoding.BUILTIN_PRESETS}
    for key, data in profili.all_dicts(KIND).items():
        found[key] = Preset(key, data["nome"], EncodingOptions.from_dict(data.get("opzioni")))
    return found


class EncodingPanel(QGroupBox):
    changed = Signal()

    def __init__(self, settings_key: str, qsettings, default: EncodingOptions | None = None):
        super().__init__("Codifica")
        self.settings_key, self.qsettings = settings_key, qsettings
        self.hw_encoders: dict[str, str | None] = {}
        self.hw_reasons: dict[str, str] = {}
        self._quiet = False
        self._build()
        saved = self._saved_options()
        self.set_options(saved if saved is not None else (default or EncodingOptions()))
        self.changed.connect(self._remember)

    # ------------------------------------------------------------------ UI
    def _build(self):
        outer = QVBoxLayout(self)
        grid = QGridLayout()
        grid.setSpacing(8)
        outer.addLayout(grid)

        grid.addWidget(QLabel("Preset:"), 0, 0)
        self.preset_bar = ProfileBar(KIND, load_presets, self._new_preset, self._rename_preset, self._update_preset,
                                     noun="preset", plural="preset")
        self.preset_bar.selected.connect(self._preset_selected)
        grid.addWidget(self.preset_bar, 0, 1, 1, 5)

        self.note_label = QLabel("")
        self.note_label.setWordWrap(True)
        self.note_label.hide()
        grid.addWidget(self.note_label, 1, 0, 1, 6)

        grid.addWidget(QLabel("Codec:"), 2, 0)
        self.codec_combo = self._combo(encoding.CODECS)
        self.codec_combo.setToolTip("«Come l'originale»: ogni file esce nello stesso codec che ha adesso "
                                    "(H.264, H.265 o AV1; se l'originale è un altro formato si usa H.265).")
        grid.addWidget(self.codec_combo, 2, 1)
        grid.addWidget(QLabel("Velocità:"), 2, 2)
        self.speed_combo = self._combo(encoding.SPEEDS)
        self.speed_combo.setToolTip("Più lenta = file più piccolo a parità di qualità.")
        grid.addWidget(self.speed_combo, 2, 3)
        grid.addWidget(QLabel("Contenitore:"), 2, 4)
        self.container_combo = self._combo(encoding.CONTAINERS)
        self.container_combo.setToolTip("«Come l'originale»: stessa estensione del file, se può contenere il codec scelto.")
        grid.addWidget(self.container_combo, 2, 5)

        grid.addWidget(QLabel("Qualità:"), 3, 0)
        self.quality_combo = QComboBox()
        for key, label, _ in encoding.QUALITY_LEVELS:
            self.quality_combo.addItem(label, key)
        for key, label in encoding.QUALITY_OTHER:
            self.quality_combo.addItem(label, key)
        self.quality_combo.currentIndexChanged.connect(self._quality_changed)
        grid.addWidget(self.quality_combo, 3, 1)
        self.quality_stack = QStackedWidget()
        self.quality_info = QLabel("")
        self.crf_spin = QSpinBox()
        self.crf_spin.setRange(0, 51)
        self.crf_spin.setPrefix("CRF ")
        self.crf_spin.setToolTip("Più basso = migliore e più grande. 18-23 sono i valori d'uso.")
        self.kbps_spin = QSpinBox()
        self.kbps_spin.setRange(100, 50000)
        self.kbps_spin.setSuffix(" kbit/s")
        for w in (self.quality_info, self.crf_spin, self.kbps_spin):
            self.quality_stack.addWidget(w)
        grid.addWidget(self.quality_stack, 3, 2, 1, 2)
        grid.addWidget(QLabel("Audio:"), 3, 4)
        self.audio_combo = self._combo(encoding.AUDIO_MODES)
        self.audio_combo.setToolTip("Se il formato originale non è ammesso dal contenitore scelto, "
                                    "viene usato automaticamente l'AAC con i canali originali.")
        grid.addWidget(self.audio_combo, 3, 5)

        self.adv_btn = QToolButton()
        self.adv_btn.setText("Avanzate")
        self.adv_btn.setCheckable(True)
        self.adv_btn.setArrowType(Qt.RightArrow)
        self.adv_btn.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.adv_btn.setAutoRaise(True)
        self.adv_btn.toggled.connect(self._toggle_advanced)
        grid.addWidget(self.adv_btn, 4, 0, 1, 2)

        self.adv = QWidget()
        adv = QGridLayout(self.adv)
        adv.setContentsMargins(0, 0, 0, 0)
        adv.setSpacing(8)
        self.tune_check = QCheckBox("Ottimizza per cartoni animati (tune animation)")
        self.tune_check.setToolTip("Solo H.264/H.265 con codifica software.")
        adv.addWidget(self.tune_check, 0, 0, 1, 3)
        self.ten_bit_check = QCheckBox("Mantieni i 10 bit se l'originale è a 10 bit")
        self.ten_bit_check.setToolTip("Solo H.265 / AV1 software; evita le bande nei gradienti.")
        adv.addWidget(self.ten_bit_check, 0, 3, 1, 3)
        self.two_pass_check = QCheckBox("Codifica a 2 passaggi (più lenta, qualità migliore a parità di dimensione)")
        self.two_pass_check.setToolTip("Solo software, con «Bitrate fisso» o «Come l'originale».")
        adv.addWidget(self.two_pass_check, 1, 0, 1, 3)
        self.cap_check = QCheckBox("Non superare il bitrate video dell'originale")
        self.cap_check.setToolTip("Con una qualità a scelta, il file non diventa mai più pesante dell'originale (H.264/H.265 software).")
        adv.addWidget(self.cap_check, 1, 3, 1, 3)
        adv.addWidget(QLabel("Deinterlaccia:"), 2, 0)
        self.deint_combo = self._combo(encoding.DEINTERLACE)
        self.deint_combo.setToolTip(
            "Il materiale delle vecchie trasmissioni TV è spesso interlacciato (righe pari e dispari di due istanti diversi) "
            "anche quando il file non lo dichiara. «Automatico» lo verifica guardando un minuto di immagini e, se serve, "
            "unisce i due semiquadri con il filtro bwdif. Se il video non è interlacciato non viene toccato.")
        adv.addWidget(self.deint_combo, 2, 1, 1, 2)
        self.gpu_check = QCheckBox("Usa la scheda video (rilevamento in corso...)")
        self.gpu_check.setEnabled(False)
        adv.addWidget(self.gpu_check, 2, 3, 1, 3)
        adv.addWidget(QLabel("Elaborazioni parallele:"), 3, 0)
        self.parallel_spin = QSpinBox()
        self.parallel_spin.setRange(1, 8)
        self.parallel_spin.setFixedWidth(60)
        adv.addWidget(self.parallel_spin, 3, 1)
        self.adv.hide()
        grid.addWidget(self.adv, 5, 0, 1, 6)
        grid.setColumnStretch(1, 1)
        grid.setColumnStretch(3, 1)
        grid.setColumnStretch(5, 1)

        for w in (self.codec_combo, self.speed_combo, self.container_combo, self.quality_combo, self.audio_combo,
                  self.deint_combo):
            w.currentIndexChanged.connect(self._edited)
        for w in (self.tune_check, self.ten_bit_check, self.two_pass_check, self.cap_check, self.gpu_check):
            w.toggled.connect(self._edited)
        for w in (self.crf_spin, self.kbps_spin, self.parallel_spin):
            w.valueChanged.connect(self._edited)

    @staticmethod
    def _combo(items) -> QComboBox:
        box = QComboBox()
        for label, value in items:
            box.addItem(label, value)
        return box

    def _toggle_advanced(self, shown: bool):
        self.adv.setVisible(shown)
        self.adv_btn.setArrowType(Qt.DownArrow if shown else Qt.RightArrow)

    # ------------------------------------------------------------------ reading and writing the options
    def options(self) -> EncodingOptions:
        return EncodingOptions(
            codec=self.codec_combo.currentData(), speed=self.speed_combo.currentData(),
            quality=self.quality_combo.currentData(), crf=self.crf_spin.value(), kbps=self.kbps_spin.value(),
            cap_bitrate=self.cap_check.isChecked(), tune_animation=self.tune_check.isChecked(),
            two_pass=self.two_pass_check.isChecked(), ten_bit=self.ten_bit_check.isChecked(),
            deinterlace=self.deint_combo.currentData(), container=self.container_combo.currentData(),
            audio=self.audio_combo.currentData(), use_gpu=self.gpu_check.isChecked() and self.gpu_check.isEnabled(),
            parallel=self.parallel_spin.value())

    def set_options(self, o: EncodingOptions) -> None:
        self._quiet = True
        try:
            for combo, value in ((self.codec_combo, o.codec), (self.speed_combo, o.speed), (self.quality_combo, o.quality),
                                 (self.container_combo, o.container), (self.audio_combo, o.audio),
                                 (self.deint_combo, o.deinterlace)):
                combo.setCurrentIndex(max(combo.findData(value), 0))
            self.crf_spin.setValue(o.crf)
            self.kbps_spin.setValue(o.kbps)
            for check, value in ((self.cap_check, o.cap_bitrate), (self.tune_check, o.tune_animation),
                                 (self.two_pass_check, o.two_pass), (self.ten_bit_check, o.ten_bit),
                                 (self.gpu_check, o.use_gpu)):
                check.setChecked(value)
            self.parallel_spin.setValue(o.parallel)
        finally:
            self._quiet = False
        self._refresh()
        self.changed.emit()

    def _saved_options(self) -> EncodingOptions | None:
        try:
            text = str(self.qsettings.value(f"codifica/{self.settings_key}", ""))
            return EncodingOptions.from_dict(json.loads(text)) if text else None
        except ValueError:
            return None

    def _remember(self):
        self.qsettings.setValue(f"codifica/{self.settings_key}", json.dumps(self.options().to_dict()))

    def _edited(self, *_):
        if self._quiet:
            return
        self._refresh()
        self.changed.emit()

    def _quality_changed(self, *_):
        self._refresh()

    # ------------------------------------------------------------------ what applies to what
    def set_hardware(self, encoders: dict, reasons: dict) -> None:
        """Called when the graphics card's encoders have been detected."""
        self.hw_encoders, self.hw_reasons = encoders, reasons
        self._refresh()

    def _refresh(self):
        codec = self.codec_combo.currentData()
        if self.hw_encoders:
            if codec == "source":
                found = ", ".join(e for e in self.hw_encoders.values() if e)
                why = " | ".join(r for r in self.hw_reasons.values() if r)
            else:
                found, why = self.hw_encoders.get(codec), self.hw_reasons.get(codec, "")
            was = self.gpu_check.blockSignals(True)
            self.gpu_check.setEnabled(bool(found))
            self.gpu_check.setText(f"Usa la scheda video (rilevato: {found})" if found
                                   else "Usa la scheda video (nessun encoder hardware rilevato)")
            self.gpu_check.setToolTip("" if found else why)
            if not found:
                self.gpu_check.setChecked(False)
            self.gpu_check.blockSignals(was)
        using_hw = self.gpu_check.isChecked() and self.gpu_check.isEnabled()
        quality = self.quality_combo.currentData()
        average_rate = quality in ("cbr", "original")
        self.tune_check.setEnabled(codec in ("source", "h264", "h265") and not using_hw)
        self.ten_bit_check.setEnabled(codec in ("source", "h265", "av1") and not using_hw)
        self.two_pass_check.setEnabled(average_rate and not using_hw)
        self.cap_check.setEnabled(not average_rate and codec in ("source", "h264", "h265") and not using_hw)
        if quality == "custom":
            self.quality_stack.setCurrentWidget(self.crf_spin)
        elif quality == "cbr":
            self.quality_stack.setCurrentWidget(self.kbps_spin)
        else:
            self.quality_stack.setCurrentWidget(self.quality_info)
            self.quality_info.setText("il bitrate video dell'originale" if quality == "original"
                                      else EncodingOptions(quality=quality).crf_label)

    def set_note(self, text: str) -> None:
        """A remark above the choices (e.g. what the originals are, and any advice about the codec)."""
        self.note_label.setText(text)
        self.note_label.setVisible(bool(text))

    # ------------------------------------------------------------------ presets
    def _preset_selected(self, key: str):
        preset = self.preset_bar.profiles.get(key)
        if preset is None:
            return
        o = EncodingOptions.from_dict(preset.options.to_dict())
        now = self.options()
        o.parallel, o.use_gpu = now.parallel, now.use_gpu  # a preset says how to encode, not how many at once
        self.set_options(o)

    def _ask_name(self, title: str, text: str = "") -> str | None:
        name, ok = QInputDialog.getText(self, title, "Nome del preset:", text=text)
        name = name.strip()
        if not ok or not name:
            return None
        if name.lower() in ("predefinito", "standard") or profili.name_taken(KIND, name):
            QMessageBox.warning(self, "Nome già usato", "Esiste già un preset con questo nome.")
            return None
        return name

    def _new_preset(self) -> str | None:
        name = self._ask_name("Nuovo preset")
        return profili.save(KIND, {"nome": name, "opzioni": self.options().to_dict()}) if name else None

    def _rename_preset(self, key: str) -> bool:
        preset = self.preset_bar.profiles.get(key)
        if preset is None or preset.builtin:
            return False
        name = self._ask_name("Rinomina il preset", preset.name)
        if not name:
            return False
        profili.save(KIND, dict(preset.to_dict(), nome=name), key)
        return True

    def _update_preset(self, key: str) -> bool:
        preset = self.preset_bar.profiles.get(key)
        if preset is None or preset.builtin:
            return False
        profili.save(KIND, {"nome": preset.name, "opzioni": self.options().to_dict()}, key)
        return True
