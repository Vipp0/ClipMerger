"""Scheda «Dividi puntate»: taglia un file che contiene tante puntate/sketch in file separati.

Due modi di trovare i punti di taglio:
- «nero» (profilo Standard): si taglia a metà dei fotogrammi neri che separano un pezzo dall'altro;
- «campione» (profili creati dall'utente): ogni pezzo comincia con lo stesso identico filmato (un breve stacco
  grafico che si ripete): lo si riconosce confrontando i fotogrammi con un campione salvato nel profilo e si taglia
  sullo stacco netto che lo precede. Se il titolo compare scritto sullo schermo, lo si legge (OCR di Windows) e lo
  si confronta con la lista di TMDB per dare il nome al file;
- «titoli» (profili creati dall'utente): senza campione. Ogni pezzo ha il titolo scritto sullo schermo accanto a un
  piccolo logo: si cerca il logo, si risale allo stacco grafico (cornice scura, centro luminoso) che lo precede e si
  taglia lì; apertura e finale del file si riconoscono dal buio.

Uso da riga di comando (per fare a mano il campione di un profilo):
    python dividi.py campione video.mkv INIZIO DURATA uscita.npz
"""
from __future__ import annotations

import base64
import re
import shutil
import subprocess
import sys
import tempfile
import time
import zlib
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import numpy as np

import applog
import encoding
import merger
import ocr_windows
import profili
import titoli
import utils
from utils import CREATE_NO_WINDOW

W, H, FPS = 96, 54, 10          # the small grey frames the cup is looked for in
STRIP_FROM = 0.65               # only the lower part of the frame is compared (the cup's black floor)
MATCH = 0.97                    # similarity from which a frame counts as "the cup"
MIN_RUN = 0.3                   # shortest stretch of matching frames that is kept, seconds
LONG_CUP = 3.5                  # the real cup lasts about 4.4 s; the short one used inside a sketch ~1.7 s
OPENING_MAX_START = 15.0        # a long cup this early in the file is the programme's opening, not a sketch
SNAP_RADIUS = 0.8               # the hard cut is looked for this close to where it is expected, seconds
SCENE_THRESHOLD = 0.25
MIN_PIECE = 20.0                # a shorter piece than this is flagged
CAPTION_FPS = 2.5
# titles method
MIN_CUP = 1.0                   # shortest stretch taken for the graphic animation, seconds
LONG_CUP_TITLES = 3.0           # an animation this long (or longer) is preferred as the one that opens a piece
CUP_BEFORE_TITLE = 15.0         # the animation begins at most this long before its title's logo appears
MIN_PIECE_GAP = 90.0            # a title without its animation only starts a piece this long after the previous one began
LOGO_FPS = 5
LOGO_BOX = (0.66, 0.80, 0.76, 0.93)  # where the title's small logo sits: x0, x1, y0, y1 as fractions of the frame
LOGO_SIZE = (96, 72)                 # the logo's region is looked at as an image of this size (width, height)
# the logo's shape: where it is white / orange (bit-packed, compressed, base64); a frame shows the title when its
# own white/orange pixels in that region overlap this shape well
_LOGO_WHITE = "eNpjYKAnYCSGfQCJ/QfBZv7D9ADBZqhvgLM56v8jxDn+/4CzGf7/hxjGjsLmQWYzwtk/GI7D2R8Y7JHY8khsfmxsNqB1//9DXMQCdDaMzYPKZtBHYjPYI7EZkdhQwEEEGxkIUIk9CqgJAKaqOTw="
_LOGO_ORANGE = "eNpjYBgFZALGg0icA0jsBgSTGUmciUEAzmZHUo6TfQC7Ocji/MhsBuxsdhx6GRwQTJ4D2NncB0bjmSIAAD11Clw="
LOGO_MATCH = 0.3                     # overlap (intersection over union) from which the logo is taken as present
DARK = 35.0                     # mean grey level below which a frame counts as dark (the file's ending)
BRIGHT = 70.0                   # ... and above which it counts as picture (the pieces themselves)
TAIL_BRIGHT = 2.0               # the last stretch of picture this long (or longer) is the end of the last piece
_SCENE_RE = re.compile(r"pts_time:([\d.]+)\s*\n.*?lavfi\.scene_score=([\d.]+)")


class Annullato(Exception):
    pass


class DividiError(Exception):
    pass


@dataclass(frozen=True, eq=False)
class Profile:
    """How a file is cut into pieces. «Standard» is built in; the others are made by the user (see profili.py)."""
    key: str
    name: str
    description: str = ""
    method: str = "black"       # "black": cut in the black between pieces; "sample": find a jingle that opens each piece;
                                # "titles": find the title's logo and the animation before it
    black_min: float = 0.12     # black method: shortest black stretch that counts, seconds
    min_piece: float = 30.0     # black method: shortest piece, seconds (closer cuts are ignored)
    sample: str = ""            # sample method: the sample's file name in the profile folder
    lead: float = 0.30          # sample method: seconds between the hard cut and the sharp jingle (expected)
    read_titles: bool = False   # sample method: the piece's title is written on the screen
    caption_from: float = 6.0   # the on-screen title appears this long after the cut ...
    caption_len: float = 8.0    # ... and is looked for during this long
    credits: bool = False       # the file ends with orange closing credits to leave out
    settings: dict = field(default_factory=dict)  # what the tab looked like when saved (series, quality, names...)
    builtin: bool = False

    @property
    def reads_titles(self) -> bool:
        """The piece titles are read from the picture (and checked against the series' list)."""
        return self.method == "titles" or (self.method == "sample" and self.read_titles)

    def to_dict(self) -> dict:
        return {"nome": self.name, "descrizione": self.description, "metodo": self.method,
                "nero_minimo": self.black_min, "pezzo_minimo": self.min_piece, "campione": self.sample,
                "secondi_prima_del_filmato": self.lead, "titolo_sullo_schermo": self.read_titles,
                "titolo_dopo": self.caption_from, "titolo_per": self.caption_len,
                "titoli_di_coda_arancioni": self.credits, "impostazioni": self.settings}

    @staticmethod
    def from_dict(key: str, d: dict) -> "Profile":
        def num(name: str, default: float, low: float = 0.0) -> float:
            try:
                return max(low, float(d.get(name, default)))
            except (TypeError, ValueError):
                return default
        settings = d.get("impostazioni")
        method = d.get("metodo") if d.get("metodo") in ("sample", "titles") else "black"
        return Profile(key, str(d.get("nome", key)), str(d.get("descrizione", "")), method,
                       num("nero_minimo", 0.12, 0.02), num("pezzo_minimo", 30.0, 1.0), str(d.get("campione", "")),
                       num("secondi_prima_del_filmato", 0.30), bool(d.get("titolo_sullo_schermo", False)),
                       num("titolo_dopo", 6.0), num("titolo_per", 8.0, 1.0), bool(d.get("titoli_di_coda_arancioni", False)),
                       settings if isinstance(settings, dict) else {})


STANDARD = Profile("standard", "Standard",
                   "Taglia a metà dei fotogrammi neri tra un pezzo e l'altro. I pezzi si chiamano «Parte 1», «Parte 2»...: "
                   "puoi scrivere i titoli a mano nella tabella.", builtin=True)
PROFILES = {"standard": STANDARD}  # the built-in one only


def load_profiles() -> dict[str, Profile]:
    """«Standard» first, then the user's profiles by name."""
    found = dict(PROFILES)
    for key, data in profili.all_dicts("dividi").items():
        found[key] = Profile.from_dict(key, data)
    return found


@dataclass
class Piece:
    start: float
    end: float
    cup_cut: float              # where this piece's own cup begins (the title belongs to it)
    cup_found: float = 0.0      # where the cup was seen, before snapping
    snapped: bool = True        # the cut was put on a real hard cut of the picture
    read_lines: list[str] = field(default_factory=list)
    thumb: Path | None = None
    number: int | None = None
    season: int = 0             # the season of the chosen episode (0 when none / not known)
    title: str = ""
    score: float = 0.0
    ambiguous: bool = False
    interlaced: bool = False  # the picture of its file is interlaced (old TV material)
    bars: tuple[int, int] | None = None  # green side bars: black out columns < first and >= second
    prologue_start: float | None = None  # first piece of a file: where its short untitled opening bit begins
    tail: tuple[float, float] | None = None  # the file's own ending (TV switching off + closing credits): (start, end)
    caption: tuple[float, float] | None = None  # titles method: (start, length) of the stretch where the title is on screen

    @property
    def duration(self) -> float:
        return self.end - self.start

    @property
    def sure(self) -> bool:
        return self.score >= titoli.SURE and not self.ambiguous and self.snapped and self.duration >= MIN_PIECE


@dataclass
class FileAnalysis:
    path: Path
    duration: float = 0.0
    opening_end: float = 0.0
    credits_start: float | None = None
    interlaced: bool = False
    interlace_note: str = ""  # how it was found out, for the tooltip
    tail_start: float | None = None  # where the ending begins (the "TV switching off" effect, else the credits)
    pieces: list[Piece] = field(default_factory=list)
    error: str | None = None


# ---------------------------------------------------------------------------- the cup sample
def _strip(frames: np.ndarray) -> np.ndarray:
    return frames.reshape(-1, H, W)[:, int(H * STRIP_FROM):, :]


def _norm(strips: np.ndarray) -> np.ndarray:
    a = strips.reshape(len(strips), -1).astype(np.float32)
    a -= a.mean(axis=1, keepdims=True)
    return a / np.maximum(np.linalg.norm(a, axis=1, keepdims=True), 1e-6)


def load_sample(profile: Profile) -> np.ndarray:
    path = profili.folder("dividi") / profile.sample
    try:
        with np.load(path) as data:
            return _norm(data["strips"])
    except (OSError, KeyError, ValueError) as exc:
        raise DividiError(f"Il campione del profilo «{profile.name}» non si legge ({path.name}).") from exc


def _gray_chunks(ffmpeg: str, path: Path, start: float | None = None, duration: float | None = None, cancelled=None):
    """Yields (first frame number, frames array) of the whole file (or a stretch), as small grey frames at FPS."""
    cmd = [ffmpeg, "-v", "error", "-nostdin"]
    if start is not None:
        cmd += ["-ss", f"{start:.3f}"]
    cmd += ["-i", str(path)]
    if duration is not None:
        cmd += ["-t", f"{duration:.3f}"]
    cmd += ["-an", "-sn", "-vf", f"fps={FPS},scale={W}:{H}:flags=area,format=gray", "-f", "rawvideo", "-"]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, creationflags=CREATE_NO_WINDOW)
    n = 0
    try:
        while True:
            if cancelled is not None and cancelled():
                raise Annullato()
            raw = proc.stdout.read(W * H * 1200)
            if not raw:
                break
            frames = np.frombuffer(raw, dtype=np.uint8)[: len(raw) // (W * H) * (W * H)].reshape(-1, W * H)
            yield n, frames
            n += len(frames)
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait()


def make_sample(ffmpeg: str, video: Path, start: float, duration: float, out: Path) -> int:
    """Saves the frames of `video` between `start` and `start + duration` (every 4th at 10 fps) as a profile
    sample. Returns how many frames were kept."""
    parts = [f for _, f in _gray_chunks(ffmpeg, video, start, duration)]
    if not parts:
        raise DividiError("Nessun fotogramma letto: controlla l'inizio e la durata.")
    frames = np.concatenate(parts)[::4]
    out.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(out, strips=_strip(frames).copy())
    return len(frames)


_BLACK_RE = re.compile(r"black_start:([\d.]+)\s+black_end:([\d.]+)")
_TIME_RE = re.compile(r"time=(\d+):(\d+):([\d.]+)")


def black_stretches(ffmpeg: str, path: Path, black_min: float, duration: float, progress=None, cancelled=None
                    ) -> list[tuple[float, float]]:
    """Every stretch of fully black picture lasting at least black_min seconds: (start, end)."""
    cmd = [ffmpeg, "-hide_banner", "-nostdin", "-i", str(path), "-an", "-sn", "-vf",
           f"scale=160:-2:flags=fast_bilinear,blackdetect=d={black_min:.3f}:pix_th=0.10", "-f", "null", "-"]
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, encoding="utf-8",
                            errors="replace", creationflags=CREATE_NO_WINDOW)
    found: list[tuple[float, float]] = []
    try:
        for line in proc.stderr:  # ffmpeg ends its progress lines with a carriage return: each is one line here
            if cancelled is not None and cancelled():
                raise Annullato()
            m = _BLACK_RE.search(line)
            if m:
                found.append((float(m.group(1)), float(m.group(2))))
                continue
            t = _TIME_RE.search(line)
            if t and progress and duration > 0:
                progress(min(1.0, (int(t.group(1)) * 3600 + int(t.group(2)) * 60 + float(t.group(3))) / duration))
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait()
    return found


def black_cuts(stretches: list[tuple[float, float]], duration: float, min_piece: float) -> list[float]:
    """The cut points: the middle of each black stretch, leaving out the ones too close to each other or to the
    ends of the file (so no piece is shorter than min_piece)."""
    cuts: list[float] = []
    last = 0.0
    for start, end in stretches:
        mid = round((start + end) / 2, 3)
        if end <= 1.0 or start >= duration - 1.0:
            continue  # the black at the very start/end of the file is not a separator
        if mid - last >= min_piece and duration - mid >= min_piece:
            cuts.append(mid)
            last = mid
    return cuts


def find_cups(ffmpeg: str, path: Path, sample: np.ndarray, duration: float, progress=None, cancelled=None
              ) -> list[tuple[float, float, float]]:
    """Every stretch where the picture is the cup: (start, end, best similarity), times in seconds."""
    on: list[bool] = []
    sims: list[float] = []
    for first, frames in _gray_chunks(ffmpeg, path, cancelled=cancelled):
        sim = (_norm(_strip(frames)) @ sample.T).max(axis=1)
        sims.extend(sim.tolist())
        if progress and duration > 0:
            progress(min(1.0, (first + len(frames)) / FPS / duration))
    on = [s > MATCH for s in sims]
    runs: list[tuple[float, float, float]] = []
    i = 0
    while i < len(on):
        if on[i]:
            j = i
            while j + 1 < len(on) and on[j + 1]:
                j += 1
            runs.append((i / FPS, (j + 1) / FPS, max(sims[i:j + 1])))
            i = j + 1
        else:
            i += 1
    return [r for r in runs if r[1] - r[0] >= MIN_RUN]


def scene_cut_near(ffmpeg: str, path: Path, t: float, radius: float = SNAP_RADIUS) -> float | None:
    """Time of the strongest hard cut of the picture within `radius` seconds of `t`, else None."""
    start = max(t - radius, 0.0)
    proc = subprocess.run(
        [ffmpeg, "-hide_banner", "-ss", f"{start:.3f}", "-t", f"{2 * radius:.3f}", "-i", str(path), "-an", "-sn",
         "-vf", f"select='gt(scene,{SCENE_THRESHOLD})',metadata=print", "-f", "null", "-"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", creationflags=CREATE_NO_WINDOW,
    )
    cuts = [(float(a) + start, float(b)) for a, b in _SCENE_RE.findall(proc.stderr)]
    return max(cuts, key=lambda c: c[1])[0] if cuts else None


_IDET_RE = re.compile(r"Multi frame detection:\s*TFF:\s*(\d+)\s*BFF:\s*(\d+)\s*Progressive:\s*(\d+)")


def detect_interlaced(ffmpeg: str, path: Path, flagged: bool, duration: float) -> tuple[bool, str]:
    """Is the picture interlaced? Old TV material often is without saying so in the file, so besides the
    file's own flag a minute of picture from the first third is looked at (ffmpeg's idet filter)."""
    if flagged:
        return True, "il file lo dichiara interlacciato"
    start = max(duration * 0.3 - 30.0, 0.0)
    proc = subprocess.run(
        [ffmpeg, "-hide_banner", "-nostdin", "-ss", f"{start:.1f}", "-t", "60", "-i", str(path), "-an", "-sn",
         "-vf", "idet", "-f", "null", "-"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", creationflags=CREATE_NO_WINDOW)
    found = _IDET_RE.findall(proc.stderr)
    if not found:
        return False, "non verificabile"
    tff, bff, prog = (int(x) for x in found[-1])
    total = tff + bff + prog
    if total == 0:
        return False, "non verificabile"
    share = (tff + bff) / total
    return share >= 0.5, f"{share * 100:.0f}% dei fotogrammi analizzati risulta interlacciato"


@lru_cache(maxsize=4)
def _has_filter(ffmpeg: str, name: str) -> bool:
    try:
        out = subprocess.run([ffmpeg, "-hide_banner", "-filters"], capture_output=True, text=True, encoding="utf-8",
                             errors="replace", timeout=15, creationflags=CREATE_NO_WINDOW).stdout
    except (subprocess.SubprocessError, OSError):
        return False
    return any(len(parts) > 1 and parts[1] == name for parts in (line.split() for line in out.splitlines()))


def deint_filter(ffmpeg: str) -> str:
    """The deinterlacer: bwdif (sharper than yadif, one frame per frame, no frame-rate change) when ffmpeg has it."""
    return "bwdif=mode=send_frame:parity=auto:deint=all" if _has_filter(ffmpeg, "bwdif") else ""


def detect_bars(ffmpeg: str, path: Path, start: float, end: float, width: int) -> tuple[int, int] | None:
    """Some sketches have dark green bars on the sides of the picture where the others have black ones (it is in the
    source). Looks at four frames of the stretch for columns that are pure green; returns where to black out
    (columns before the first / from the second value on), or None when the bars are black as they should be."""
    length = end - start - 6.0
    if length < 4 or width < 64:
        return None
    proc = subprocess.run(
        [ffmpeg, "-v", "error", "-nostdin", "-ss", f"{start + 3.0:.2f}", "-t", f"{length:.2f}", "-i", str(path), "-an", "-sn",
         "-vf", f"fps={4 / length:.5f},scale={width}:6:flags=area,format=rgb24", "-frames:v", "4", "-f", "rawvideo", "-"],
        capture_output=True, creationflags=CREATE_NO_WINDOW)
    n = len(proc.stdout) // (width * 6 * 3)
    if n < 2:
        return None
    cols = np.frombuffer(proc.stdout[: n * width * 6 * 3], dtype=np.uint8).reshape(n, 6, width, 3).astype(int).mean(axis=(0, 1))
    green = (cols[:, 0] < 8) & (cols[:, 1] > 40) & (cols[:, 2] > 15)
    left = [x for x in np.where(green)[0] if x < width * 0.25]
    right = [x for x in np.where(green)[0] if x > width * 0.75]
    if len(left) < 4 or len(right) < 4:
        return None
    return int(max(left)) + 3, int(min(right)) - 2


def _snap(ffmpeg: str, path: Path, t: float, radius: float) -> float:
    cut = scene_cut_near(ffmpeg, path, t, radius)
    return round(cut, 3) if cut is not None else t


def tv_off_start(ffmpeg: str, path: Path, credits: float) -> float | None:
    """Where the "television switching off" effect that closes the last sketch begins (a hard cut to nearly black
    frames, 0.3-0.4 s before the credits), or None when the sketch goes straight to the credits."""
    cut = scene_cut_near(ffmpeg, path, credits - 0.4, 0.3)
    if cut is None or not (0.1 <= credits - cut <= 0.7):
        return None
    proc = subprocess.run(
        [ffmpeg, "-v", "error", "-nostdin", "-ss", f"{cut + 0.04:.3f}", "-i", str(path), "-an", "-sn", "-frames:v", "1",
         "-vf", "scale=32:18,format=gray", "-f", "rawvideo", "-"],
        capture_output=True, creationflags=CREATE_NO_WINDOW)
    if len(proc.stdout) < 32 * 18 or np.frombuffer(proc.stdout, dtype=np.uint8).mean() > 25:
        return None  # what follows the cut is not dark: an ordinary cut inside the sketch
    return round(cut, 3)


def credits_start(ffmpeg: str, path: Path, duration: float) -> float | None:
    """Where the orange closing credits begin (the last stretch where most of the picture is orange)."""
    t0 = max(0.0, duration - 40.0)
    proc = subprocess.run(
        [ffmpeg, "-v", "error", "-nostdin", "-ss", f"{t0:.2f}", "-i", str(path), "-an", "-sn",
         "-vf", "fps=5,scale=32:18,format=rgb24", "-f", "rawvideo", "-"],
        capture_output=True, creationflags=CREATE_NO_WINDOW)
    raw = proc.stdout
    if len(raw) < 32 * 18 * 3 * 10:
        return None
    fr = np.frombuffer(raw[: len(raw) // (32 * 18 * 3) * (32 * 18 * 3)], dtype=np.uint8).reshape(-1, 18 * 32, 3).astype(int)
    orange = ((fr[:, :, 0] > 200) & (fr[:, :, 1] > 120) & (fr[:, :, 1] < 200) & (fr[:, :, 2] < 80)).mean(axis=1) > 0.25
    for i in range(len(orange)):
        if orange[i:].mean() > 0.9:
            return t0 + i / 5
    return None


# ---------------------------------------------------------------------------- titles on the screen
def _yellow_mask_filter() -> str:
    cond = "gt(r(X,Y),140)*gt(g(X,Y),140)*lt(b(X,Y),120)*gt(r(X,Y)-b(X,Y),70)"
    expr = f"if({cond},0,255)"
    return f"format=gbrp,geq=g='{expr}':b='{expr}':r='{expr}',format=rgb24"


def _caption_frames(ffmpeg: str, path: Path, piece_no: int, cut: float, profile: Profile, folder: Path,
                    window: tuple[float, float] | None = None) -> None:
    """Writes the frames where the title may be (normal, and with only the yellow lettering kept) as pictures.
    `window` = (start, length) when it is known exactly; else it is looked for after the cut as the profile says."""
    start, length = window if window else (cut + profile.caption_from, profile.caption_len)
    flt = ("[0:v]fps=%s,crop=iw:ih*0.30:0:ih*0.70,scale=iw*2:-1:flags=lanczos,split[a][b];[b]%s[m]"
           % (CAPTION_FPS * 2 if window else CAPTION_FPS, _yellow_mask_filter()))  # a known window is read more densely
    subprocess.run(
        [ffmpeg, "-v", "error", "-y", "-nostdin", "-ss", f"{max(start, 0.0):.3f}", "-t", f"{length:.2f}",
         "-i", str(path), "-an", "-sn", "-filter_complex", flt,
         "-map", "[a]", str(folder / f"p{piece_no:03d}_%02d.png"),
         "-map", "[m]", str(folder / f"p{piece_no:03d}_%02dm.png")],
        capture_output=True, creationflags=CREATE_NO_WINDOW)


def read_titles(ffmpeg: str, path: Path, pieces: list[Piece], first_no: int, profile: Profile, folder: Path,
                catalogue: list[titoli.Episodio], used: set[int], cancelled=None) -> None:
    """Fills number/title/score of every piece of one file from the caption shown after its cup."""
    for k, piece in enumerate(pieces):
        if cancelled is not None and cancelled():
            raise Annullato()
        _caption_frames(ffmpeg, path, first_no + k, piece.cup_cut, profile, folder, piece.caption)
    texts = ocr_windows.read_folder(folder)
    for k, piece in enumerate(pieces):
        prefix = f"p{first_no + k:03d}_"
        lines = [line for name, ls in sorted(texts.items()) if name.startswith(prefix) for line in ls]
        piece.read_lines = lines
        shots = sorted(folder.glob(f"{prefix}[0-9][0-9].png"))
        match = titoli.best_match(lines, catalogue, used)
        piece.number, piece.title, piece.score, piece.ambiguous = match.number, match.title, match.score, match.ambiguous
        piece.season = match.season

        def shows_title(shot: Path) -> float:  # how well this frame's lettering fits the chosen title (else: how much there is)
            read = texts.get(shot.stem, [])
            fit = max((titoli.similarity(t, piece.title) for t in read), default=0.0) if piece.title else 0.0
            return fit * 1000 + len(" ".join(read))
        piece.thumb = max(shots, key=shows_title) if shots else None
        if piece.number is not None:
            used.add((piece.season, piece.number))


# ---------------------------------------------------------------------------- titles method
def _runs(flags: np.ndarray, rate: float, min_len: float, join: float = 0.0) -> list[tuple[float, float]]:
    """Stretches (start, end) in seconds where `flags` is true; gaps shorter than `join` seconds are bridged."""
    out: list[list[int]] = []
    i, n = 0, len(flags)
    while i < n:
        if flags[i]:
            j = i
            while j + 1 < n and flags[j + 1]:
                j += 1
            if out and (i - out[-1][1] - 1) / rate <= join:
                out[-1][1] = j
            else:
                out.append([i, j])
            i = j + 1
        else:
            i += 1
    return [(a / rate, (b + 1) / rate) for a, b in out if (b + 1 - a) / rate >= min_len]


def scan_titles(ffmpeg: str, path: Path, duration: float, progress=None, cancelled=None):
    """One look at the whole file: (animations, brightness, logos).
    animations: stretches where the picture has a dark frame at its sides and a bright centre (the graphic that opens
    every piece, in any of its versions, and also the opening of the file); brightness: mean grey level of every frame
    at FPS; logos: stretches where the title's small logo (white with orange lettering) is at the lower right."""
    means: list[np.ndarray] = []
    side: list[np.ndarray] = []
    x = lambda a: int(a * W)
    y = lambda a: int(a * H)
    for first, frames in _gray_chunks(ffmpeg, path, cancelled=cancelled):
        f = frames.reshape(-1, H, W).astype(np.float32)
        left = f[:, y(0.10):y(0.70), x(0.15):x(0.185)].mean(axis=(1, 2))
        right = f[:, y(0.10):y(0.70), x(0.815):x(0.85)].mean(axis=(1, 2))
        centre = f[:, y(0.25):y(0.60), x(0.38):x(0.62)].mean(axis=(1, 2))
        side.append((left < 60) & (right < 60) & (centre > 70))
        means.append(f.mean(axis=(1, 2)))
        if progress:
            progress(min(1.0, (first + len(frames)) / FPS / max(duration, 1.0)))
    brightness = np.concatenate(means) if means else np.zeros(0)
    cups = _runs(np.concatenate(side) if side else np.zeros(0, dtype=bool), FPS, MIN_CUP, join=0.2)

    x0, x1, y0, y1 = LOGO_BOX
    rw, rh = LOGO_SIZE
    proc = subprocess.run(
        [ffmpeg, "-v", "error", "-nostdin", "-i", str(path), "-an", "-sn", "-vf",
         f"fps={LOGO_FPS},crop=iw*{x1 - x0}:ih*{y1 - y0}:iw*{x0}:ih*{y0},scale={rw}:{rh},format=rgb24",
         "-f", "rawvideo", "-"], capture_output=True, creationflags=CREATE_NO_WINDOW)
    n = len(proc.stdout) // (rw * rh * 3)
    px = np.frombuffer(proc.stdout[: n * rw * rh * 3], dtype=np.uint8).reshape(n, rw * rh, 3).astype(np.int32)
    r, g, b = px[:, :, 0], px[:, :, 1], px[:, :, 2]
    orange = (r > 190) & (g > 80) & (g < 175) & (b < 80) & (r - g > 50)
    white = (r > 205) & (g > 205) & (b > 190)

    def shape(b64: str) -> np.ndarray:
        bits = np.unpackbits(np.frombuffer(zlib.decompress(base64.b64decode(b64)), dtype=np.uint8))
        return bits[: rw * rh].astype(bool).reshape(1, rh * rw)
    tw, to = shape(_LOGO_WHITE), shape(_LOGO_ORANGE)
    inter = (white & tw).sum(axis=1) + (orange & to).sum(axis=1)
    union = (white | tw).sum(axis=1) + (orange | to).sum(axis=1)
    logos = _runs(inter / np.maximum(union, 1) >= LOGO_MATCH, LOGO_FPS, 1.6, join=1.9)
    return cups, brightness, logos


def _opening_end(ffmpeg: str, path: Path, cups, brightness: np.ndarray, logos) -> float:
    """Where the programme's opening ends: the last graphic animation in the first seconds that no title follows
    (an animation that does have a title after it opens a piece), then, if the screen goes dark right after it (the
    lettering on black), up to the first picture. 0 when the file does not begin with an opening."""
    early = [c for c in cups if c[0] <= OPENING_MAX_START and not any(c[0] <= lg[0] <= c[0] + CUP_BEFORE_TITLE for lg in logos)]
    if not early:
        return 0.0
    end = early[-1][1]
    j = int(end * FPS)
    while j < len(brightness) and j / FPS < end + 1.5 and brightness[j] >= DARK:
        j += 1
    if j < len(brightness) and brightness[j] < DARK:  # the dark screen follows: wait for the picture
        while j < len(brightness) and brightness[j] < BRIGHT * 0.7 and j / FPS < end + 30.0:
            j += 1
        end = j / FPS
    return _snap(ffmpeg, path, end, 0.5)


def _ending_start(ffmpeg: str, path: Path, brightness: np.ndarray, duration: float) -> float | None:
    """Where the file's own ending (lettering on black, credits) begins: after the last stretch of real picture."""
    bright = brightness > BRIGHT
    lasts = _runs(bright, FPS, TAIL_BRIGHT, join=0.5)
    if not lasts:
        return None
    end = lasts[-1][1]
    if not (3.0 <= duration - end <= 120.0):
        return None
    return _snap(ffmpeg, path, end, 0.5)


def _titles_pieces(ffmpeg: str, path: Path, res: "FileAnalysis", cups, brightness, logos, orange_credits: bool) -> None:
    res.opening_end = _opening_end(ffmpeg, path, cups, brightness, logos)
    if orange_credits:  # closing credits on an orange background, with the television switching off before them
        found = credits_start(ffmpeg, path, res.duration)
        if found:
            res.credits_start = _snap(ffmpeg, path, found, 0.5)
            res.tail_start = tv_off_start(ffmpeg, path, res.credits_start) or res.credits_start
    if res.tail_start is None:  # lettering and credits on black (also when orange ones were asked for but not there)
        res.tail_start = _ending_start(ffmpeg, path, brightness, res.duration)
    end_of_file = res.tail_start or res.duration
    starts: list[tuple[float, bool, float, tuple[float, float]]] = []  # (cut, snapped, cup seen at, title window)
    taken: set[int] = set()
    for lg in logos:
        near = [(k, c) for k, c in enumerate(cups) if k not in taken and c[0] >= res.opening_end - 0.5
                and lg[0] - CUP_BEFORE_TITLE <= c[0] and c[1] <= lg[0] + 0.5]
        long_ones = [kc for kc in near if kc[1][1] - kc[1][0] >= LONG_CUP_TITLES]
        pick = (long_ones or near or [None])[-1]
        window = (lg[0] - 1.0, lg[1] - lg[0] + 2.0)  # the lettering can come a little before / go on after the logo
        if pick is None:
            if starts and lg[0] - starts[-1][0] < MIN_PIECE_GAP:
                continue  # a title this soon after the last piece began is not a new piece
            starts.append((max(lg[0] - 8.0, res.opening_end), False, 0.0, window))
            continue
        taken.add(pick[0])
        seen = pick[1][0]
        cut = scene_cut_near(ffmpeg, path, seen, SNAP_RADIUS)
        starts.append((round(cut, 3) if cut is not None else seen, cut is not None, seen, window))
    if not starts:
        res.error = "Non trovo nessun titolo con il suo logo in questo file: il profilo non lo riconosce."
        return
    bounds = [res.opening_end] + [s[0] for s in starts[1:]] + [end_of_file]
    for i, (cut, ok, seen, window) in enumerate(starts):
        res.pieces.append(Piece(
            start=bounds[i], end=max(bounds[i + 1], bounds[i] + 0.1), cup_cut=cut, cup_found=seen, snapped=ok,
            prologue_start=bounds[i] if i == 0 and bounds[i] < cut - 1.0 else None, interlaced=res.interlaced,
            tail=(res.tail_start, res.duration) if res.tail_start else None, caption=window))


# ---------------------------------------------------------------------------- analysis of one file
def analizza_file(ffmpeg: str, ffprobe: str, path: Path, profile: Profile, sample: np.ndarray,
                  catalogue: list[titoli.Episodio], used: set[int], work: Path, first_no: int,
                  progress=None, cancelled=None) -> FileAnalysis:
    """progress(fraction 0..1, text). Pieces and their titles; nothing is written but pictures in `work`."""
    res = FileAnalysis(path)
    info = merger.probe(ffprobe, path)
    res.duration = info.duration
    res.interlaced, res.interlace_note = detect_interlaced(ffmpeg, path, info.interlaced, info.duration)
    if profile.method == "black":
        if progress:
            progress(0.0, f"{path.name}: cerco i fotogrammi neri")
        stretches = black_stretches(
            ffmpeg, path, profile.black_min, res.duration,
            progress=(lambda f: progress(f * 0.9, f"{path.name}: cerco i fotogrammi neri")) if progress else None,
            cancelled=cancelled)
        bounds = [0.0] + black_cuts(stretches, res.duration, profile.min_piece) + [res.duration]
        applog.log.info("Dividi: %s -> %d neri, %d pezzi", path.name, len(stretches), len(bounds) - 1)
        for k in range(len(bounds) - 1):
            res.pieces.append(Piece(start=bounds[k], end=bounds[k + 1], cup_cut=bounds[k], title=f"Parte {first_no + k}",
                                    score=1.0, interlaced=res.interlaced))
        for piece in res.pieces:
            piece.bars = detect_bars(ffmpeg, path, piece.start, piece.end, info.width)
        if progress:
            progress(1.0, f"{path.name}: fatto")
        return res
    if profile.method == "titles":
        if progress:
            progress(0.0, f"{path.name}: cerco i titoli e gli stacchi")
        cups_t, brightness, logos = scan_titles(
            ffmpeg, path, res.duration, progress=(lambda f: progress(f * 0.7, f"{path.name}: cerco i titoli e gli stacchi")) if progress else None,
            cancelled=cancelled)
        applog.log.info("Dividi: %s -> %d stacchi, %d titoli", path.name, len(cups_t), len(logos))
        _titles_pieces(ffmpeg, path, res, cups_t, brightness, logos, profile.credits)
        if res.error:
            return res
        for piece in res.pieces:
            piece.bars = detect_bars(ffmpeg, path, piece.start, piece.end, info.width)
        if progress:
            progress(0.8, f"{path.name}: leggo i titoli")
        read_titles(ffmpeg, path, res.pieces, first_no, profile, work, catalogue, used, cancelled)
        if progress:
            progress(1.0, f"{path.name}: fatto")
        return res
    if progress:
        progress(0.0, f"{path.name}: cerco il filmato di stacco")
    cups = find_cups(ffmpeg, path, sample, res.duration,
                     progress=(lambda f: progress(f * 0.8, f"{path.name}: cerco il filmato di stacco")) if progress else None,
                     cancelled=cancelled)
    long_cups = [c for c in cups if c[1] - c[0] >= LONG_CUP]
    applog.log.info("Dividi: %s -> %d stacchi lunghi, %d brevi", path.name, len(long_cups), len(cups) - len(long_cups))
    if long_cups and long_cups[0][0] <= OPENING_MAX_START:
        res.opening_end = _snap(ffmpeg, path, long_cups[0][1], 0.5)  # the opening ends on a hard cut too
        long_cups = long_cups[1:]
    if not long_cups:
        res.error = "Il filmato del profilo non compare in questo file: il profilo non lo riconosce."
        return res
    if profile.credits:
        res.credits_start = credits_start(ffmpeg, path, res.duration)
        if res.credits_start:
            res.credits_start = _snap(ffmpeg, path, res.credits_start, 0.5)
    res.tail_start = res.credits_start
    if res.credits_start:
        res.tail_start = tv_off_start(ffmpeg, path, res.credits_start) or res.credits_start
    end_of_file = res.tail_start or res.duration

    if progress:
        progress(0.8, f"{path.name}: punti di taglio")
    cuts = []
    for s, e, m in long_cups:
        if cancelled is not None and cancelled():
            raise Annullato()
        expected = max(s - profile.lead, 0.0)
        snapped = scene_cut_near(ffmpeg, path, expected)
        cuts.append((round(snapped, 3) if snapped is not None else expected, snapped is not None, s))
    starts = [res.opening_end] + [c[0] for c in cuts[1:]]
    for i, (cut, ok, seen) in enumerate(cuts):
        end = starts[i + 1] if i + 1 < len(starts) else end_of_file
        res.pieces.append(Piece(start=starts[i], end=end, cup_cut=cut, cup_found=seen, snapped=ok,
                                prologue_start=starts[i] if i == 0 and starts[i] < cut - 1.0 else None,
                                interlaced=res.interlaced,
                                tail=(res.tail_start, res.duration) if res.tail_start else None))

    for piece in res.pieces:
        piece.bars = detect_bars(ffmpeg, path, piece.start, piece.end, info.width)
    if profile.read_titles:
        if progress:
            progress(0.9, f"{path.name}: leggo i titoli")
        read_titles(ffmpeg, path, res.pieces, first_no, profile, work, catalogue, used, cancelled)
    else:
        for k, piece in enumerate(res.pieces):
            piece.title, piece.score = f"Parte {first_no + k}", 1.0
    if progress:
        progress(1.0, f"{path.name}: fatto")
    return res


def analizza(files: list[Path], profile: Profile, catalogue: list[titoli.Episodio], ffmpeg: str, ffprobe: str,
             work: Path, progress=None, cancelled=None) -> list[FileAnalysis]:
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True, exist_ok=True)
    sample = load_sample(profile) if profile.method == "sample" else None
    out: list[FileAnalysis] = []
    used: set[int] = set()
    first_no = 1
    for i, f in enumerate(files):
        def report(frac: float, text: str, i=i):
            if cancelled is not None and cancelled():
                raise Annullato()
            if progress:
                progress((i + frac) / len(files), text)
        folder = work / f"file{i:02d}"  # one folder per file, so that each OCR run only reads that file's pictures
        folder.mkdir(parents=True, exist_ok=True)
        try:
            res = analizza_file(ffmpeg, ffprobe, f, profile, sample, catalogue, used, folder, first_no, report, cancelled)
        except Annullato:
            raise
        except Exception as exc:  # one bad file must not stop the others
            applog.log_exception("Dividi puntate (analisi)", exc)
            res = FileAnalysis(f, error=applog.describe_exception(exc))
        first_no += len(res.pieces)
        out.append(res)
    if progress:
        progress(1.0, "Analisi completata")
    return out


# ---------------------------------------------------------------------------- cutting
def cut_piece(ffmpeg: str, ffprobe: str, src: Path, start: float, end: float, output: Path,
              opts: "encoding.EncodingOptions", hw_encoders: dict | None = None, title: str = "", progress_cb=None,
              cancel_event=None, tail: tuple[float, float] | None = None, deinterlace: bool = False,
              bars: tuple[int, int] | None = None) -> merger.MergeResult:
    """One piece: [start, end) of `src`, re-encoded once as `opts` says (by default in the same codec and container,
    never above the source's video bitrate). `deinterlace` merges the fields (one frame per frame pair, sharp bwdif
    filter); whether to do it is the caller's decision. With `tail` = (start, end) of the same file, that stretch
    (the file's own ending) is joined after the piece, in the same single encode."""
    try:
        info = merger.probe(ffprobe, src)
        info.interlaced = deinterlace  # the caller decides, whatever the file's own flag says
        settings, notes = encoding.build_settings(opts, info, hw_encoders, ffmpeg)
        settings.deinterlace = True
        suffix, changed = merger.pick_container(src.suffix, settings.codec, opts.container)
        output = output.with_suffix(suffix)
        if output.exists():
            return merger.MergeResult(output_path=output, success=True, skipped=True)
        output.parent.mkdir(parents=True, exist_ok=True)
        segments = [(src, info)] + ([(src, info)] if tail else [])
        windows = {0: (start, end - 0.01)}
        if tail:
            windows[1] = tail
        prefilters = None
        if bars:  # black out the green side bars of the piece (not of the ending taken from elsewhere in the file)
            prefilters = {0: f"drawbox=x=0:y=0:w={bars[0]}:h=ih:color=black:t=fill,"
                             f"drawbox=x={bars[1]}:y=0:w=iw-{bars[1]}:h=ih:color=black:t=fill"}
        def add_title(cmd: list[str]) -> list[str]:
            if title:
                at = len(cmd) - 1 - cmd[::-1].index("-progress")
                cmd[at:at] = ["-metadata", f"title={title}"]
            return cmd
        tmp = output.with_name(f".{output.stem}.part{output.suffix}")
        try:
            expected = end - start + ((tail[1] - tail[0]) if tail else 0.0)
            merger.run_encode(ffmpeg, segments, 0, tmp, settings, expected, progress_cb, cancel_event,
                              edit_command=add_title, windows=windows, prefilters=prefilters)
            merger.verify_output(ffprobe, tmp, expected)
            tmp.replace(output)
        finally:
            tmp.unlink(missing_ok=True)
        if changed:
            notes.append(f"salvato come {suffix}: il formato {src.suffix} non supporta questo codec")
        return merger.MergeResult(output_path=output, success=True, note="; ".join(notes) or None)
    except merger.Cancelled:
        return merger.MergeResult(output_path=output, success=False, error="Annullato")
    except (merger.MergeError, DividiError) as exc:
        return merger.MergeResult(output_path=output, success=False, error=str(exc))


def _main(argv: list[str]) -> int:
    if len(argv) == 6 and argv[1] == "campione":
        n = make_sample("ffmpeg", Path(argv[2]), float(argv[3]), float(argv[4]), Path(argv[5]))
        print(f"{n} fotogrammi salvati in {argv[5]}")
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(_main(sys.argv))
