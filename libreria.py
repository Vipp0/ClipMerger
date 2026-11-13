"""La libreria: l'elenco dei video importati, uguale per tutti gli strumenti.

Si importa una volta sola (cartelle o file); i file restano anche dopo aver chiuso il programma. Appena un file
entra, in secondo piano ne vengono lette durata, video, tracce audio e sottotitoli (lettura rapida, un secondo
per file): il risultato è tenuto da parte e rifatto solo se il file cambia. Le analisi lunghe (trovare le
sigle, i tagli...) le fa lo strumento scelto, non la libreria."""
from __future__ import annotations

import json
import os
import statistics
from dataclasses import dataclass, field
from pathlib import Path

from PySide6.QtCore import QObject, QThread, Signal

import applog
import dropwidgets
import merger


def store_file() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home())
    d = Path(base) / "ClipMerger"
    d.mkdir(parents=True, exist_ok=True)
    return d / "libreria.json"


@dataclass
class Item:
    path: Path
    selected: bool = True
    info: dict | None = None    # what the quick read found (see quick_info)
    error: str = ""             # why the quick read failed
    missing: bool = False       # the file is no longer where it was

    @property
    def name(self) -> str:
        return self.path.name


def _stamp(path: Path) -> tuple[int, float] | None:
    try:
        st = path.stat()
        return st.st_size, st.st_mtime
    except OSError:
        return None


def quick_info(ffprobe: str, path: Path) -> dict:
    """What the library shows about a file: one fast ffprobe run."""
    p = merger.probe(ffprobe, path)
    size, mtime = _stamp(path) or (0, 0.0)
    return {
        "size": size, "mtime": mtime, "duration": p.duration, "vcodec": p.video_codec, "width": p.width,
        "height": p.height, "fps": p.fps, "vbitrate": p.video_bitrate, "interlaced": p.interlaced, "bit_depth": p.bit_depth,
        "audio": [{"lang": a.language or "und", "codec": a.codec, "ch": a.channels} for a in p.audio],
        "subs": len(p.subtitles),
    }


class ProbeWorker(QThread):
    """Reads the files it is given, one after the other, and reports each (the file's path, its info or why not)."""
    read = Signal(str, object, str)

    def __init__(self, ffprobe: str, paths: list[Path]):
        super().__init__()
        self.ffprobe, self.paths = ffprobe, paths

    def run(self):
        for path in self.paths:
            if self.isInterruptionRequested():
                return
            try:
                self.read.emit(str(path), quick_info(self.ffprobe, path), "")
            except Exception as exc:  # a file that cannot be read must not stop the others
                self.read.emit(str(path), None, applog.describe_exception(exc))


class Library(QObject):
    changed = Signal()        # the list or the selection changed
    item_read = Signal(str)   # the quick read of this file (path) is in
    reading = Signal(int, int)  # files still to read, files in total of this round

    def __init__(self, ffprobe_getter):
        super().__init__()
        self.ffprobe_getter = ffprobe_getter
        self.items: list[Item] = []
        self.worker: ProbeWorker | None = None
        self.retired: list[ProbeWorker] = []
        self._pending = 0
        self._round_total = 0

    # ------------------------------------------------------------------ the list
    def find(self, path: str | Path) -> Item | None:
        path = Path(path)
        return next((i for i in self.items if i.path == path), None)

    def add_paths(self, paths: list[Path]) -> int:
        """Imports videos (a folder gives the videos in it); new ones are selected. Returns how many were new."""
        known = {i.path: i for i in self.items}
        added, reselected = [], False
        for f in dropwidgets.video_files([Path(p) for p in paths]):
            if f in known:  # already there: importing it again just selects it
                reselected = reselected or not known[f].selected
                known[f].selected = True
            else:
                known[f] = Item(f)
                added.append(known[f])
        if added or reselected:
            self.items += added
            self.changed.emit()
            self.save()
            self._read([i.path for i in added])
        return len(added)

    def remove(self, paths: list[Path]) -> None:
        gone = set(paths)
        self.items = [i for i in self.items if i.path not in gone]
        self.changed.emit()
        self.save()

    def clear(self) -> None:
        self.items = []
        self.changed.emit()
        self.save()

    def set_selected(self, path: Path, selected: bool) -> None:
        item = self.find(path)
        if item is not None and item.selected != selected:
            item.selected = selected
            self.changed.emit()
            self.save()

    def select_all(self, selected: bool) -> None:
        for i in self.items:
            i.selected = selected
        self.changed.emit()
        self.save()

    def selected_files(self) -> list[Path]:
        return [i.path for i in self.items if i.selected and not i.missing]

    # ------------------------------------------------------------------ the quick read
    def _read(self, paths: list[Path]) -> None:
        ffprobe = self.ffprobe_getter()
        if not ffprobe or not paths:
            return
        self._pending += len(paths)
        self._round_total += len(paths)
        old = self.worker
        if old is not None and old.isRunning():  # let it finish what it has; the new files get their own worker
            self.retired.append(old)
        self.retired = [w for w in self.retired if w.isRunning()]
        self.worker = ProbeWorker(ffprobe, paths)
        self.worker.read.connect(self._on_read)
        self.worker.start()
        self.reading.emit(self._pending, self._round_total)

    def _on_read(self, path: str, info, error: str) -> None:
        item = self.find(path)
        self._pending = max(self._pending - 1, 0)
        if item is not None:
            item.info, item.error = info, error
            if info is None:
                item.error = error or "Non riesco a leggere questo file."
        if self._pending == 0:
            self._round_total = 0
        self.reading.emit(self._pending, self._round_total)
        if item is not None:
            self.item_read.emit(path)
            self.save()

    def shutdown(self) -> None:
        for w in [self.worker, *self.retired]:
            if w is not None and w.isRunning():
                w.requestInterruption()
                w.wait(5000)

    # ------------------------------------------------------------------ remembering it
    def save(self) -> None:
        data = {"versione": 1, "file": [
            {"path": str(i.path), "selezionato": i.selected, "info": i.info} for i in self.items]}
        try:
            store_file().write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        except OSError as exc:
            applog.log.warning("Libreria non salvata: %s", exc)

    def load(self) -> None:
        """The files of the last session. A file whose size or date changed is read again; a missing one is marked."""
        try:
            data = json.loads(store_file().read_text(encoding="utf-8"))
            entries = data["file"]
        except (OSError, ValueError, KeyError, TypeError):
            return
        to_read = []
        for e in entries:
            try:
                item = Item(Path(e["path"]), bool(e.get("selezionato", True)))
            except (KeyError, TypeError):
                continue
            stamp = _stamp(item.path)
            if stamp is None:
                item.missing = True
                item.info = e.get("info") if isinstance(e.get("info"), dict) else None
            else:
                info = e.get("info")
                if isinstance(info, dict) and (info.get("size"), info.get("mtime")) == stamp:
                    item.info = info
                else:
                    to_read.append(item.path)
            self.items.append(item)
        if self.items:
            self.changed.emit()
        self._read(to_read)


# ---------------------------------------------------------------------------- suggestions
def is_sigla_name(name: str) -> bool:
    n = name.lower()
    return "sigla" in n and ("iniziale" in n or "finale" in n)


def describe_video(info: dict) -> str:
    codec = {"h264": "H.264", "hevc": "H.265", "av1": "AV1", "mpeg4": "MPEG-4", "mpeg2video": "MPEG-2"}.get(
        info.get("vcodec", ""), (info.get("vcodec") or "?").upper())
    fps = info.get("fps") or 0
    return f"{codec} · {info.get('width', 0)}×{info.get('height', 0)} · {fps:.4g} fps" + (" · interlacciato" if info.get("interlaced") else "")


def describe_audio(info: dict) -> str:
    return ", ".join(f"{a['lang']} {a['codec']}" for a in info.get("audio", [])) or "nessuna"


@dataclass
class Suggestion:
    text: str
    tools: list[str] = field(default_factory=list)  # keys of the tools worth opening


def suggestions(items: list[Item]) -> list[Suggestion]:
    """A few remarks about what is selected, with the tools they point at. Only guesses: never an action by itself."""
    chosen = [i for i in items if i.selected and not i.missing and i.info]
    episodes = [i for i in chosen if not is_sigla_name(i.name)]
    out: list[Suggestion] = []
    if not episodes:
        return out
    long_ones = [i for i in episodes if i.info["duration"] > 45 * 60]
    others = [i for i in episodes if i not in long_ones]
    if long_ones and (len(episodes) == 1 or (others and statistics.median(i.info["duration"] for i in others) < 40 * 60)):
        i = long_ones[0]
        h, m = divmod(int(i.info["duration"]) // 60, 60)
        out.append(Suggestion(f"«{i.name}» dura {h}:{m:02d} ore: potrebbe contenere più puntate.", ["dividi"]))
    if len(episodes) >= 3:
        med = statistics.median(i.info["duration"] for i in episodes)
        similar = [i for i in episodes if abs(i.info["duration"] - med) <= med * 0.35]
        sizes = {(i.info["width"], i.info["height"]) for i in similar}
        if len(similar) >= 3 and len(sizes) == 1:
            w, h = next(iter(sizes))
            out.append(Suggestion(
                f"Sembra una serie: {len(similar)} episodi di circa {round(med / 60)} minuti, tutti {w}×{h}. "
                "Per cambiare le sigle: «Sostituisci sigla» se hanno già le loro, «Unisci sigle» per aggiungerne.",
                ["sostituisci", "unisci"]))
    layouts = {tuple(a["lang"] for a in i.info["audio"]) for i in episodes}
    if len(episodes) >= 2 and len(layouts) > 1:
        out.append(Suggestion("Le tracce audio non sono uguali in tutti i file.", ["tracce"]))
    n_interlaced = sum(1 for i in episodes if i.info.get("interlaced"))
    if n_interlaced:
        out.append(Suggestion(
            f"{n_interlaced} file {'è dichiarato interlacciato' if n_interlaced == 1 else 'sono dichiarati interlacciati'}: "
            "in «Codifica → Avanzate» la deinterlaccia è su Automatico.", []))
    return out[:3]
