"""Scheda «Dividi puntate»: taglia un file che contiene tante puntate/sketch in file separati.

Primo profilo: «Sketch senza sigle» (Camera Café). Ogni sketch comincia con lo stesso identico filmato
(un bicchiere che fa apparire il titolo): lo si riconosce confrontando i fotogrammi con un campione, si
taglia sullo stacco netto che lo precede, e il titolo scritto sullo schermo (letto con l'OCR di Windows e
confrontato con la lista di TMDB) dà il nome al file. Niente sigle: si scartano l'apertura e i titoli di coda.

Uso da riga di comando (solo per fare un campione per un nuovo profilo):
    python dividi.py campione video.mkv INIZIO DURATA uscita.npz
"""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

import applog
import merger
import ocr_windows
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
_SCENE_RE = re.compile(r"pts_time:([\d.]+)\s*\n.*?lavfi\.scene_score=([\d.]+)")


class Annullato(Exception):
    pass


class DividiError(Exception):
    pass


@dataclass(frozen=True)
class Profile:
    key: str
    name: str
    sample: str                 # file in assets/profili
    lead: float                 # seconds between the hard cut and the sharp cup (expected)
    caption_from: float = 6.0   # the on-screen title appears this long after the cut ...
    caption_len: float = 8.0    # ... and is looked for during this long
    series_id: int | None = None
    credits: bool = True        # the file ends with orange closing credits to leave out


PROFILES = {
    "camera_cafe": Profile("camera_cafe", "Sketch senza sigle (Camera Café)", "camera_cafe.npz", 0.30,
                           series_id=63809),
}


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
    title: str = ""
    score: float = 0.0
    ambiguous: bool = False

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
    path = Path(utils.resource_path(f"assets/profili/{profile.sample}"))
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


def _snap(ffmpeg: str, path: Path, t: float, radius: float) -> float:
    cut = scene_cut_near(ffmpeg, path, t, radius)
    return round(cut, 3) if cut is not None else t


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


def _caption_frames(ffmpeg: str, path: Path, piece_no: int, cut: float, profile: Profile, folder: Path) -> None:
    """Writes the frames where the title may be (normal, and with only the yellow lettering kept) as pictures."""
    start = cut + profile.caption_from
    flt = ("[0:v]fps=%s,crop=iw:ih*0.30:0:ih*0.70,scale=iw*2:-1:flags=lanczos,split[a][b];[b]%s[m]"
           % (CAPTION_FPS, _yellow_mask_filter()))
    subprocess.run(
        [ffmpeg, "-v", "error", "-y", "-nostdin", "-ss", f"{start:.3f}", "-t", f"{profile.caption_len:.2f}",
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
        _caption_frames(ffmpeg, path, first_no + k, piece.cup_cut, profile, folder)
    texts = ocr_windows.read_folder(folder)
    for k, piece in enumerate(pieces):
        prefix = f"p{first_no + k:03d}_"
        lines = [line for name, ls in sorted(texts.items()) if name.startswith(prefix) for line in ls]
        piece.read_lines = lines
        shots = sorted(folder.glob(f"{prefix}[0-9][0-9].png"))
        match = titoli.best_match(lines, catalogue, used)
        piece.number, piece.title, piece.score, piece.ambiguous = match.number, match.title, match.score, match.ambiguous

        def shows_title(shot: Path) -> float:  # how well this frame's lettering fits the chosen title (else: how much there is)
            read = texts.get(shot.stem, [])
            fit = max((titoli.similarity(t, piece.title) for t in read), default=0.0) if piece.title else 0.0
            return fit * 1000 + len(" ".join(read))
        piece.thumb = max(shots, key=shows_title) if shots else None
        if piece.number is not None:
            used.add(piece.number)


# ---------------------------------------------------------------------------- analysis of one file
def analizza_file(ffmpeg: str, ffprobe: str, path: Path, profile: Profile, sample: np.ndarray,
                  catalogue: list[titoli.Episodio], used: set[int], work: Path, first_no: int,
                  progress=None, cancelled=None) -> FileAnalysis:
    """progress(fraction 0..1, text). Pieces and their titles; nothing is written but pictures in `work`."""
    res = FileAnalysis(path)
    info = merger.probe(ffprobe, path)
    res.duration = info.duration
    if progress:
        progress(0.0, f"{path.name}: cerco i bicchieri")
    cups = find_cups(ffmpeg, path, sample, res.duration,
                     progress=(lambda f: progress(f * 0.8, f"{path.name}: cerco i bicchieri")) if progress else None,
                     cancelled=cancelled)
    long_cups = [c for c in cups if c[1] - c[0] >= LONG_CUP]
    applog.log.info("Dividi: %s -> %d bicchieri lunghi, %d brevi", path.name, len(long_cups), len(cups) - len(long_cups))
    if long_cups and long_cups[0][0] <= OPENING_MAX_START:
        res.opening_end = _snap(ffmpeg, path, long_cups[0][1], 0.5)  # the opening ends on a hard cut too
        long_cups = long_cups[1:]
    if not long_cups:
        res.error = "Nessun bicchiere trovato: il profilo non riconosce questo file."
        return res
    if profile.credits:
        res.credits_start = credits_start(ffmpeg, path, res.duration)
        if res.credits_start:
            res.credits_start = _snap(ffmpeg, path, res.credits_start, 0.5)
    end_of_file = res.credits_start or res.duration

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
        res.pieces.append(Piece(start=starts[i], end=end, cup_cut=cut, cup_found=seen, snapped=ok))

    if progress:
        progress(0.9, f"{path.name}: leggo i titoli")
    read_titles(ffmpeg, path, res.pieces, first_no, profile, work, catalogue, used, cancelled)
    if progress:
        progress(1.0, f"{path.name}: fatto")
    return res


def analizza(files: list[Path], profile: Profile, catalogue: list[titoli.Episodio], ffmpeg: str, ffprobe: str,
             work: Path, progress=None, cancelled=None) -> list[FileAnalysis]:
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True, exist_ok=True)
    sample = load_sample(profile)
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
QUALITY_LEVELS = [("Alta", 18), ("Bilanciata (consigliata)", 20), ("Compatta", 23)]


def cut_piece(ffmpeg: str, ffprobe: str, src: Path, start: float, end: float, output: Path, crf: int, preset: str,
              codec: str, title: str = "", progress_cb=None, cancel_event=None) -> merger.MergeResult:
    """One piece: [start, end) of `src`, re-encoded once, in the same container, never above the source's video
    bitrate. The audio keeps its codec and track layout."""
    try:
        info = merger.probe(ffprobe, src)
        use = codec if codec != "original" else (merger.app_codec_for(info.video_codec) or "h264")
        suffix, changed = merger.pick_container(src.suffix, use)
        output = output.with_suffix(suffix)
        if output.exists():
            return merger.MergeResult(output_path=output, success=True, skipped=True)
        output.parent.mkdir(parents=True, exist_ok=True)
        cap = (info.video_bitrate // 1000) if info.video_bitrate else 0
        settings = merger.MergeSettings(
            codec=use, encoder=utils.CODEC_ENCODERS[use][0], is_hardware=False, preset=preset,
            bitrate_mode="crf", crf=crf + (-1 if use == "h265" else 0), audio_mode="original",
            max_kbps=cap if use in ("h264", "h265") else 0,
        )
        cmd = merger.build_command(ffmpeg, [(src, info)], 0, output, settings, windows={0: (start, end - 0.01)})
        if title:
            at = len(cmd) - 1 - cmd[::-1].index("-progress")
            cmd[at:at] = ["-metadata", f"title={title}"]
        tmp = output.with_name(f".{output.stem}.part{output.suffix}")
        cmd[-1] = str(tmp)
        try:
            merger.run_merge_pass(cmd, end - start, progress_cb, cancel_event)
            merger.verify_output(ffprobe, tmp, end - start)
            tmp.replace(output)
        finally:
            tmp.unlink(missing_ok=True)
        note = f"salvato come {suffix}: il formato {src.suffix} non supporta questo codec" if changed else None
        return merger.MergeResult(output_path=output, success=True, note=note)
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
