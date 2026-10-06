"""Audio-based theme-song finder: where does a known sigla sit inside an episode?

No GUI here (the tab uses it, and so does the command line):

    python trova_sigla.py cerca  episodio.mkv sigla.wav [--zona apertura|chiusura|tutto] [--minuti 6]
    python trova_sigla.py impara cartella_episodi       [--zona ...] [--salva sigla_appresa.wav]

How it works, in short: the audio of the search zone and of the reference is turned
into a sequence of spectral "frames" (log energy in a few bands, then differenced in
time, which cancels volume and static equalisation changes). The reference is cut
into blocks of ~5 s and each block is looked for in the zone with a normalised
cross-correlation done through the FFT. Blocks that agree on the same position vote
for it, which also copes with sigle that are shortened or cut in some episodes. The
exact edges are then found by following the frame-by-frame similarity along that
alignment. Only numpy is needed (plus ffmpeg/ffprobe on the PATH).
"""
from __future__ import annotations

import argparse
import math
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

SAMPLE_RATE = 8000
FRAME = 512  # 64 ms at 8 kHz
FINE_HOP = 80  # 10 ms: the precision of the reported times
COARSE_HOP = 320  # 40 ms: used only by the folder-wide "impara" vote, for speed
N_BANDS = 24
DELTA = 2  # frames of the time difference (20 ms fine / 80 ms coarse)

BLOCK_SECONDS = 5.0
BLOCK_STEP_SECONDS = 2.5
MIN_BLOCK_SCORE = 0.30  # normalised correlation below this is not a match
SMOOTH_SECONDS = 0.30  # smoothing of the per-frame similarity used to find the edges


class SiglaError(Exception):
    pass


# --------------------------------------------------------------------------- results
@dataclass
class Detection:
    status: str  # "trovata" | "non_trovata" | "piu_volte"
    start: float | None = None  # seconds in the episode
    end: float | None = None
    confidence: int = 0  # 0..100
    ref_start: float = 0.0  # where in the reference the matched part begins
    ref_end: float = 0.0
    alternatives: list[tuple[float, float, int]] = field(default_factory=list)  # (start, end, confidence)

    def __str__(self) -> str:
        if self.start is None:
            return f"{self.status} (affidabilità {self.confidence})"
        extra = f" | altre posizioni: {self.alternatives}" if self.alternatives else ""
        return (
            f"{self.status}: {self.start:.2f} -> {self.end:.2f} s "
            f"(durata {self.end - self.start:.2f}, affidabilità {self.confidence}, "
            f"parte del campione {self.ref_start:.2f}-{self.ref_end:.2f}){extra}"
        )


@dataclass
class LearnResult:
    seed: Path
    start: float  # the learned sigla inside the seed episode, in seconds
    end: float
    detections: dict[Path, Detection]
    voters: int


# --------------------------------------------------------------------------- tools
def _find(name: str) -> str:
    path = shutil.which(name)
    if not path:
        raise SiglaError(f"{name} non trovato nel PATH")
    return path


def _duration(ffprobe: str, path: Path) -> float:
    proc = subprocess.run(
        [ffprobe, "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, creationflags=CREATE_NO_WINDOW,
    )
    try:
        return float(proc.stdout.strip())
    except ValueError as exc:
        raise SiglaError(f"durata non leggibile: {path.name}") from exc


def extract_audio(ffmpeg: str, path: Path, start: float = 0.0, duration: float | None = None,
                  track: int = 0) -> np.ndarray:
    """Mono 8 kHz float32 audio of `path` (optionally just a window of it)."""
    cmd = [ffmpeg, "-v", "error", "-nostdin"]
    if start > 0:
        cmd += ["-ss", f"{start:.3f}"]
    cmd += ["-i", str(path)]
    if duration is not None:
        cmd += ["-t", f"{duration:.3f}"]
    cmd += ["-map", f"0:a:{track}", "-vn", "-ac", "1", "-ar", str(SAMPLE_RATE), "-f", "s16le", "-"]
    proc = subprocess.run(cmd, capture_output=True, creationflags=CREATE_NO_WINDOW)
    if proc.returncode != 0 or not proc.stdout:
        raise SiglaError(f"audio non estraibile da {path.name}: {proc.stderr.decode(errors='replace')[-200:]}")
    return np.frombuffer(proc.stdout, dtype=np.int16).astype(np.float32) / 32768.0


# --------------------------------------------------------------------------- features
def _band_matrix() -> np.ndarray:
    freqs = np.arange(FRAME // 2 + 1) * SAMPLE_RATE / FRAME
    edges = np.geomspace(150.0, 3800.0, N_BANDS + 1)
    matrix = np.zeros((len(freqs), N_BANDS), dtype=np.float32)
    for b in range(N_BANDS):
        sel = (freqs >= edges[b]) & (freqs < edges[b + 1])
        if sel.any():
            matrix[sel, b] = 1.0 / sel.sum()
    return matrix


_BANDS = _band_matrix()
_WINDOW = np.hanning(FRAME).astype(np.float32)


def spectral(audio: np.ndarray, hop: int) -> tuple[np.ndarray, np.ndarray]:
    """(features, level). features: (frames, bands) time-differenced log band energies;
    level: (frames,) mean log energy, used to tell silence from sound. Frame t describes
    the audio starting at t*hop samples; the same mapping is used for reference and episode."""
    if len(audio) < FRAME + hop:
        audio = np.pad(audio, (0, FRAME + hop - len(audio)))
    n_frames = 1 + (len(audio) - FRAME) // hop
    log_e = np.empty((n_frames, N_BANDS), dtype=np.float32)
    chunk = 2048
    for i0 in range(0, n_frames, chunk):
        i1 = min(n_frames, i0 + chunk)
        idx = np.arange(i0, i1)[:, None] * hop + np.arange(FRAME)[None, :]
        spec = np.abs(np.fft.rfft(audio[idx] * _WINDOW, axis=1)) ** 2
        log_e[i0:i1] = np.log(spec @ _BANDS + 1e-7)
    return log_e[DELTA:] - log_e[:-DELTA], log_e[:-DELTA].mean(axis=1)


def features(audio: np.ndarray, hop: int) -> np.ndarray:
    return spectral(audio, hop)[0]


def _standardise(feats: np.ndarray, scale: np.ndarray) -> np.ndarray:
    return feats / scale


def _band_scale(feats: np.ndarray) -> np.ndarray:
    return np.maximum(feats.std(axis=0), 1e-3).astype(np.float32)


# --------------------------------------------------------------------------- matching
class _Zone:
    """A search zone whose spectra are precomputed once and reused for every block."""

    def __init__(self, feats: np.ndarray, level: np.ndarray, max_block_frames: int):
        self.n = len(feats)
        self.level = level
        self.scale = _band_scale(feats)
        self.feats = _standardise(feats, self.scale)
        self.size = 1 << int(math.ceil(math.log2(self.n + max_block_frames + 1)))
        self.spectra = np.fft.rfft(self.feats, n=self.size, axis=0)
        energy = np.concatenate([[0.0], np.cumsum((self.feats.astype(np.float64) ** 2).sum(axis=1))])
        self.energy = energy

    def correlate(self, block: np.ndarray) -> np.ndarray:
        """Normalised cross-correlation of `block` (already standardised) at every lag."""
        m = len(block)
        if m > self.n:
            return np.zeros(0, dtype=np.float32)
        block_spec = np.fft.rfft(block, n=self.size, axis=0)
        corr = np.fft.irfft((self.spectra * np.conj(block_spec)).sum(axis=1), n=self.size)[: self.n - m + 1]
        window_energy = self.energy[m:] - self.energy[: self.n - m + 1]
        norm = math.sqrt(float((block.astype(np.float64) ** 2).sum())) * np.sqrt(np.maximum(window_energy, 1e-9))
        return (corr / np.maximum(norm, 1e-9)).astype(np.float32)


def _top_peaks(ncc: np.ndarray, count: int, separation: int, floor: float) -> list[tuple[int, float]]:
    peaks = []
    work = ncc.copy()
    for _ in range(count):
        if work.size == 0:
            break
        i = int(work.argmax())
        if work[i] < floor:
            break
        peaks.append((i, float(work[i])))
        work[max(0, i - separation): i + separation + 1] = -1.0
    return peaks


def _block_starts(n_frames: int, hop_seconds: float) -> list[tuple[int, int]]:
    length = max(1, int(round(BLOCK_SECONDS / hop_seconds)))
    step = max(1, int(round(BLOCK_STEP_SECONDS / hop_seconds)))
    if n_frames <= length * 1.4:
        return [(0, n_frames)]
    starts = list(range(0, n_frames - length + 1, step))
    if starts[-1] + length < n_frames:
        starts.append(n_frames - length)
    return [(s, s + length) for s in starts]


def _frame_similarity(ref: np.ndarray, zone: np.ndarray, offset: int) -> np.ndarray:
    """Cosine similarity, frame by frame, of the reference against the zone at `offset`;
    NaN where the alignment falls outside the zone."""
    n = len(ref)
    sim = np.full(n, np.nan, dtype=np.float32)
    lo, hi = max(0, -offset), min(n, len(zone) - offset)
    if hi <= lo:
        return sim
    a, b = ref[lo:hi], zone[lo + offset: hi + offset]
    dot = (a * b).sum(axis=1)
    norm = np.linalg.norm(a, axis=1) * np.linalg.norm(b, axis=1)
    sim[lo:hi] = dot / np.maximum(norm, 1e-6)
    return sim


def _smooth(x: np.ndarray, width: int) -> np.ndarray:
    x = np.nan_to_num(x, nan=0.0)
    if width <= 1:
        return x
    kernel = np.ones(width, dtype=np.float32) / width
    return np.convolve(x, kernel, mode="same")


def _locate(ref: np.ndarray, zone: _Zone, hop_seconds: float, max_alternatives: int = 2):
    """Core search. Returns (best, alternatives); each is a dict with the alignment and
    the matched span in reference frames, or (None, []) if nothing convincing is there."""
    blocks = _block_starts(len(ref), hop_seconds)
    separation = max(2, int(round(1.0 / hop_seconds)))
    votes = []  # (offset, score, block index)
    for bi, (s, e) in enumerate(blocks):
        ncc = zone.correlate(ref[s:e])
        for lag, score in _top_peaks(ncc, 3, separation, MIN_BLOCK_SCORE):
            votes.append((lag - s, score, bi))
    if not votes:
        return None, []

    tol = max(2, int(round(0.06 / hop_seconds)))  # blocks agree if their implied offsets are within ~60 ms
    groups = []  # clusters of votes sharing an offset
    for vote in sorted(votes):
        if groups and vote[0] - groups[-1]["offsets"][-1] <= tol:
            groups[-1]["offsets"].append(vote[0])
            groups[-1]["votes"].append(vote)
        else:
            groups.append({"offsets": [vote[0]], "votes": [vote]})
    for g in groups:  # one vote per block (the strongest) so a block can't back an offset twice
        best_per_block = {}
        for v in g["votes"]:
            if v[2] not in best_per_block or v[1] > best_per_block[v[2]][1]:
                best_per_block[v[2]] = v
        g["votes"] = list(best_per_block.values())
        g["support"] = len(g["votes"])
        g["mean"] = float(np.mean([v[1] for v in g["votes"]]))
        g["offset"] = int(round(float(np.median([v[0] for v in g["votes"]]))))
    needed = 1 if len(blocks) == 1 else 2
    groups = [g for g in groups if g["support"] >= needed]
    if not groups:
        return None, []
    groups.sort(key=lambda g: (g["support"], g["mean"]), reverse=True)

    smooth_w = max(3, int(round(SMOOTH_SECONDS / hop_seconds)))

    def describe(g):
        offset = g["offset"]
        sim = _smooth(_frame_similarity(ref, zone.feats, offset), smooth_w)
        core_frames = np.concatenate([np.arange(blocks[v[2]][0], blocks[v[2]][1]) for v in g["votes"]])
        core_frames = core_frames[(core_frames + offset >= 0) & (core_frames + offset < zone.n)]
        if core_frames.size == 0:
            return None
        threshold = 0.5 * float(np.median(sim[core_frames]))
        anchor = int(np.median(core_frames))
        lo = hi = anchor
        while lo > 0 and sim[lo - 1] >= threshold:
            lo -= 1
        while hi < len(sim) - 1 and sim[hi + 1] >= threshold:
            hi += 1
        # the smoothing window centres its transition on the edge (see window_s in _detection_from)
        coverage = (hi - lo + 1) / len(ref)
        quality = min(1.0, max(0.0, (g["mean"] - 0.25) / 0.55))
        confidence = int(round(100 * quality * (0.5 + 0.5 * min(1.0, coverage))))
        return {"offset": offset, "lo": lo, "hi": hi + 1, "confidence": confidence, "support": g["support"],
                "quality": quality, "lo_cut": lo > 0, "hi_cut": hi < len(sim) - 1}

    best = describe(groups[0])
    if best is None:
        return None, []
    alternatives = []
    ref_len = len(ref)
    for g in groups[1:]:
        if len(alternatives) >= max_alternatives:
            break
        far_enough = abs(g["offset"] - best["offset"]) > max(ref_len * 0.5, 50)
        if far_enough and g["support"] >= max(2, 0.5 * groups[0]["support"]):
            d = describe(g)
            if d is not None and d["confidence"] >= 30:
                alternatives.append(d)
    return best, alternatives


# --------------------------------------------------------------------------- public API
def _zone_window(ffprobe: str, path: Path, zone: str, minutes: float) -> tuple[float, float | None]:
    if zone == "tutto":
        return 0.0, None
    seconds = minutes * 60.0
    if zone == "apertura":
        return 0.0, seconds
    if zone == "chiusura":
        total = _duration(ffprobe, path)
        start = max(0.0, total - seconds)
        return start, total - start
    raise SiglaError(f"zona sconosciuta: {zone}")


def cerca(episode: Path, sample: Path | np.ndarray, zone: str = "apertura", minutes: float = 6.0,
          track: int = 0, ffmpeg: str | None = None, ffprobe: str | None = None) -> Detection:
    """Find `sample` (a file, or already-extracted 8 kHz audio) inside `episode`."""
    ffmpeg, ffprobe = ffmpeg or _find("ffmpeg"), ffprobe or _find("ffprobe")
    sample_audio = sample if isinstance(sample, np.ndarray) else extract_audio(ffmpeg, Path(sample))
    start, length = _zone_window(ffprobe, Path(episode), zone, minutes)
    zone_audio = extract_audio(ffmpeg, Path(episode), start, length, track)
    return _detect(sample_audio, zone_audio, start)


def _detect(sample_audio: np.ndarray, zone_audio: np.ndarray, zone_start: float) -> Detection:
    hop_s = FINE_HOP / SAMPLE_RATE
    ref, ref_level = spectral(sample_audio, FINE_HOP)
    zone_feats, zone_level = spectral(zone_audio, FINE_HOP)
    zone = _Zone(zone_feats, zone_level, len(ref))
    return _detection_from(ref, ref_level, zone, hop_s, zone_start)


def _extend_through_silence(d: dict, ref_level: np.ndarray, zone_level: np.ndarray) -> None:
    """A sigla often ends (or starts) with silence that the spectral match can't see.
    Where BOTH the reference and the episode are silent next to the matched part, that
    silence belongs to the sigla; real sound in the episode stops the extension."""
    offset, lo, hi = d["offset"], d["lo"], d["hi"]
    core = ref_level[lo:hi]
    if core.size == 0:
        return
    silent_below = float(np.median(core)) - 7.0

    def silent(t: int) -> bool:
        z = t + offset
        return 0 <= z < len(zone_level) and ref_level[t] < silent_below and zone_level[z] < silent_below

    while hi < len(ref_level) and silent(hi):
        hi += 1
    while lo > 0 and silent(lo - 1):
        lo -= 1
    d["lo"], d["hi"] = lo, hi
    d["lo_cut"], d["hi_cut"] = lo > 0, hi < len(ref_level)
    d["confidence"] = int(round(100 * d["quality"] * (0.5 + 0.5 * min(1.0, (hi - lo) / len(ref_level)))))


def _detection_from(ref: np.ndarray, ref_level: np.ndarray, zone: _Zone, hop_s: float,
                    zone_start: float) -> Detection:
    ref = _standardise(ref, zone.scale)
    best, alternatives = _locate(ref, zone, hop_s)
    if best is None or best["confidence"] < 25:
        return Detection("non_trovata", confidence=best["confidence"] if best else 0)
    _extend_through_silence(best, ref_level, zone.level)
    for alt in alternatives:
        _extend_through_silence(alt, ref_level, zone.level)

    window_s = (FRAME + DELTA * round(hop_s * SAMPLE_RATE)) / SAMPLE_RATE  # audio covered by one frame

    def span(d):
        # A frame starts at its index*hop but covers `window_s` of audio. An edge found by the
        # similarity curve crosses at the centre of that window; one bounded by the reference
        # itself (its first or last frame) is exact: the end is where the last frame stops.
        start = zone_start + (d["offset"] + d["lo"]) * hop_s + (window_s / 2 if d["lo_cut"] else 0.0)
        end = zone_start + (d["offset"] + d["hi"] - 1) * hop_s + (window_s / 2 if d["hi_cut"] else window_s)
        return start, end

    start, end = span(best)
    result = Detection(
        "trovata", start=start, end=end, confidence=best["confidence"],
        ref_start=best["lo"] * hop_s + (window_s / 2 if best["lo_cut"] else 0.0),
        ref_end=(best["hi"] - 1) * hop_s + (window_s / 2 if best["hi_cut"] else window_s),
    )
    if alternatives:
        result.status = "piu_volte"
        result.alternatives = [(*span(d), d["confidence"]) for d in alternatives]
    return result


def impara(episodes: list[Path], zone: str = "apertura", minutes: float = 6.0, track: int = 0,
           ffmpeg: str | None = None, ffprobe: str | None = None, progress=None) -> LearnResult:
    """Learn the sigla from the episodes themselves: it is the stretch of audio they
    all have in common. Needs at least 3 episodes."""
    episodes = [Path(p) for p in episodes]
    if len(episodes) < 3:
        raise SiglaError("servono almeno 3 episodi per imparare la sigla")
    ffmpeg, ffprobe = ffmpeg or _find("ffmpeg"), ffprobe or _find("ffprobe")

    def report(done: int, total: int, what: str):
        if progress:
            progress(done, total, what)

    total_steps = len(episodes) * 2 + 1
    audios, starts = [], []
    for i, ep in enumerate(episodes):
        report(i, total_steps, f"Lettura audio: {ep.name}")
        s, length = _zone_window(ffprobe, ep, zone, minutes)
        audios.append(extract_audio(ffmpeg, ep, s, length, track))
        starts.append(s)

    coarse_hop_s = COARSE_HOP / SAMPLE_RATE
    coarse = [spectral(a, COARSE_HOP) for a in audios]  # (features, level)

    # Voters: a handful of other episodes, evenly spread, are enough to see what recurs.
    def voters_for(seed: int) -> list[int]:
        others = [i for i in range(len(episodes)) if i != seed]
        if len(others) > 8:
            others = [others[round(k * (len(others) - 1) / 7)] for k in range(8)]
        return others

    best = None  # (run_seconds * mean_support, seed, first_block_start_frame, last_block_end_frame)
    for seed in sorted({0, len(episodes) // 2, len(episodes) - 1}):
        voters = voters_for(seed)
        need = max(2, math.ceil(0.6 * len(voters)))
        seed_feats = coarse[seed][0]
        blocks = _block_starts(len(seed_feats), coarse_hop_s)
        zones = []
        for v in voters:
            zones.append(_Zone(coarse[v][0], coarse[v][1], max(e - s for s, e in blocks)))
        support = []
        for s, e in blocks:
            count = 0
            for z in zones:
                ncc = z.correlate(_standardise(seed_feats[s:e], z.scale))
                if ncc.size and float(ncc.max()) >= 0.45:
                    count += 1
            support.append(count)
        # longest run of consecutive blocks that most voters also contain
        run_start, run_best = None, None
        for bi, count in enumerate(support + [0]):
            if count >= need:
                run_start = bi if run_start is None else run_start
            elif run_start is not None:
                run = (bi - run_start, run_start, bi - 1)
                if run_best is None or run[0] > run_best[0]:
                    run_best = run
                run_start = None
        if run_best is None:
            continue
        n_blocks, first, last = run_best
        score = (blocks[last][1] - blocks[first][0]) * float(np.mean(support[first:last + 1]))
        if best is None or score > best[0]:
            best = (score, seed, blocks[first][0], blocks[last][1], len(voters))
    if best is None:
        raise SiglaError("nessuna sigla in comune trovata negli episodi")

    _, seed, first_frame, last_frame, n_voters = best
    # Rough template = that stretch of the seed plus a margin, refined below.
    margin = int(round(2.0 / coarse_hop_s))
    t0 = max(0, first_frame - margin) * COARSE_HOP
    t1 = min(len(audios[seed]), (last_frame + margin) * COARSE_HOP + FRAME)
    rough_audio = audios[seed][t0:t1]
    hop_s = FINE_HOP / SAMPLE_RATE
    rough_feats, rough_level = spectral(rough_audio, FINE_HOP)

    fine_zones = {}
    for i in range(len(episodes)):
        zf, zl = spectral(audios[i], FINE_HOP)
        fine_zones[i] = _Zone(zf, zl, len(rough_feats))

    def detect_all(ref_feats: np.ndarray, ref_level: np.ndarray, label: str) -> dict[int, Detection]:
        out = {}
        for i, ep in enumerate(episodes):
            report(len(episodes) + i + (len(episodes) if label == "final" else 0), total_steps, f"Ricerca: {ep.name}")
            out[i] = _detection_from(ref_feats, ref_level, fine_zones[i], hop_s, starts[i])
        return out

    rough = detect_all(rough_feats, rough_level, "rough")
    good = [d for d in rough.values() if d.status != "non_trovata" and d.confidence >= 40]
    if len(good) < 2:
        raise SiglaError("la sigla appresa non è abbastanza coerente tra gli episodi")
    # Trim the template to the part most episodes agree on (in rough-template time).
    ref_start = float(np.median([d.ref_start for d in good]))
    ref_end = float(np.median([d.ref_end for d in good]))
    a, b = int(round(ref_start / hop_s)), int(round(ref_end / hop_s))
    final_feats, final_level = rough_feats[a:b], rough_level[a:b]
    if len(final_feats) < int(2.0 / hop_s):
        raise SiglaError("la sigla appresa è troppo corta per essere affidabile")
    final = detect_all(final_feats, final_level, "final")
    seed_start = starts[seed] + (t0 / SAMPLE_RATE) + ref_start
    seed_end = starts[seed] + (t0 / SAMPLE_RATE) + ref_end
    report(total_steps, total_steps, "Fatto")
    return LearnResult(
        seed=episodes[seed], start=seed_start, end=seed_end,
        detections={episodes[i]: d for i, d in final.items()}, voters=n_voters,
    )


def save_clip(ffmpeg: str, source: Path, start: float, end: float, target: Path) -> None:
    """Cut [start, end] out of `source` as a plain audio file (the learned sigla)."""
    cmd = [ffmpeg, "-v", "error", "-y", "-ss", f"{start:.3f}", "-t", f"{end - start:.3f}", "-i", str(source),
           "-vn", "-ac", "2", str(target)]
    proc = subprocess.run(cmd, capture_output=True, text=True, creationflags=CREATE_NO_WINDOW)
    if proc.returncode != 0:
        raise SiglaError(f"salvataggio fallito: {proc.stderr[-200:]}")


# --------------------------------------------------------------------------- command line
VIDEO_EXTENSIONS = {".mp4", ".mkv", ".avi", ".mov", ".ts", ".m2ts", ".wmv", ".flv", ".webm", ".mpg", ".mpeg"}


def _main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Trova una sigla dentro gli episodi.")
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name in ("cerca", "impara"):
        p = sub.add_parser(name)
        if name == "cerca":
            p.add_argument("episodio", type=Path)
            p.add_argument("sigla", type=Path)
        else:
            p.add_argument("cartella", type=Path)
            p.add_argument("--salva", type=Path, help="salva la sigla appresa in questo file audio")
        p.add_argument("--zona", choices=("apertura", "chiusura", "tutto"), default="apertura")
        p.add_argument("--minuti", type=float, default=6.0)
        p.add_argument("--traccia", type=int, default=0, help="indice della traccia audio (0 = la prima)")
    args = parser.parse_args(argv)

    if args.cmd == "cerca":
        print(cerca(args.episodio, args.sigla, args.zona, args.minuti, args.traccia))
        return 0

    episodes = sorted(p for p in args.cartella.iterdir() if p.suffix.lower() in VIDEO_EXTENSIONS and not p.name.startswith("."))
    print(f"{len(episodes)} episodi in {args.cartella}")
    result = impara(episodes, args.zona, args.minuti, args.traccia,
                    progress=lambda d, t, what: print(f"  [{d}/{t}] {what}", file=sys.stderr))
    print(f"Sigla appresa dall'episodio '{result.seed.name}': {result.start:.2f} -> {result.end:.2f} s "
          f"(durata {result.end - result.start:.2f}, {result.voters} episodi a confronto)")
    for ep, det in result.detections.items():
        print(f"  {ep.name}: {det}")
    if args.salva:
        save_clip(_find("ffmpeg"), result.seed, result.start, result.end, args.salva)
        print(f"Salvata in {args.salva}")
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
