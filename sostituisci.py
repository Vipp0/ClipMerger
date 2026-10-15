"""Modalità B della scheda "Sostituisci sigla": toglie la sigla che c'è già dentro un episodio
(iniziale e/o finale) e ci mette quella nuova, audio e video, con una sola ricodifica.

Uso da riga di comando (prima versione, solo per prove):
    python sostituisci.py episodio.mkv --iniziale nuova_iniziale.mkv --campione-iniziale vecchia_iniziale.wav
                          --finale nuova_finale.mkv --campione-finale vecchia_finale.wav -o uscita.mkv
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import merger
import trova_sigla
import utils
from utils import CREATE_NO_WINDOW

_BLACK_RE = re.compile(r"black_start:([\d.]+) black_end:([\d.]+)")


class SostituisciError(Exception):
    pass


@dataclass
class CutPlan:
    """Which part of the episode survives: [keep_start, keep_end] in the episode's own time."""
    keep_start: float  # where the old opening's animation stops (what follows, e.g. an episode-title card, stays)
    keep_end: float  # where the old closing credits begin
    mute_head: float  # seconds, from keep_start, whose sound is the old opening's music tail: silenced
    old_intro: trova_sigla.Detection | None
    old_outro: trova_sigla.Detection | None


def black_intervals(ffmpeg: str, path: Path, start: float, duration: float) -> list[tuple[float, float]]:
    """Stretches of fully black picture inside [start, start + duration] (times in the file)."""
    start = max(start, 0.0)
    proc = subprocess.run(
        [ffmpeg, "-hide_banner", "-ss", f"{start:.3f}", "-t", f"{duration:.3f}", "-i", str(path),
         "-an", "-vf", "blackdetect=d=0.1:pix_th=0.1", "-f", "null", "-"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", creationflags=CREATE_NO_WINDOW,
    )
    return [(float(a) + start, float(b) + start) for a, b in _BLACK_RE.findall(proc.stderr)]


def plan_cuts(
    ffmpeg: str, episode: Path, duration: float,
    intro: trova_sigla.Detection | None, outro: trova_sigla.Detection | None,
) -> CutPlan:
    """Turns the audio detections into picture cut points, snapping to the black frames the
    sigle are bracketed with. The opening is cut where its animation ends (its last black
    frame before the music ends), so an episode-title card that follows is kept, without the
    music; the closing is cut inside the black that precedes the credits."""
    keep_start, mute_head = 0.0, 0.0
    if intro is not None:
        blacks = black_intervals(ffmpeg, episode, intro.start - 1.0, intro.end - intro.start + 8.0)
        inside = [b for b in blacks if intro.start + 1.0 < b[0] <= intro.end]
        keep_start = inside[-1][0] if inside else intro.end
        mute_head = max(intro.end - keep_start, 0.0)
    keep_end = duration
    if outro is not None:
        keep_end = outro.start
        blacks = black_intervals(ffmpeg, episode, outro.start - 8.0, 10.0)
        near = [b for b in blacks if b[0] <= outro.start + 0.5 and b[1] >= outro.start - 3.0]
        if near:
            b0, b1 = near[-1]
            keep_end = b0 + min(0.25, (b1 - b0) / 2)
    if keep_end <= keep_start + 60:
        raise SostituisciError("I punti di taglio trovati non sono coerenti: controlla le sigle rilevate.")
    return CutPlan(keep_start, keep_end, mute_head, intro, outro)


def replace_sigle(
    ffmpeg: str, ffprobe: str, episode: Path, new_intro: Path | None, new_outro: Path | None,
    plan: CutPlan, output: Path, settings: merger.MergeSettings,
    progress_cb=None, cancel_event=None,
) -> merger.ProbeResult:
    """Builds and runs the single re-encode: new opening + kept part of the episode + new closing."""
    ep = merger.probe(ffprobe, episode)
    segments: list[tuple[Path, merger.ProbeResult]] = []
    if new_intro is not None:
        segments.append((new_intro, merger.probe(ffprobe, new_intro)))
    episode_idx = len(segments)
    segments.append((episode, ep))
    if new_outro is not None:
        segments.append((new_outro, merger.probe(ffprobe, new_outro)))

    trims = {episode_idx: (plan.keep_start, plan.keep_end)}
    mutes = {episode_idx: plan.mute_head} if plan.mute_head > 0 else None
    cmd = merger.build_command(ffmpeg, segments, episode_idx, output, settings, trims=trims, mutes=mutes)
    expected = sum(p.duration for i, (_, p) in enumerate(segments) if i != episode_idx) + (plan.keep_end - plan.keep_start)
    tmp = output.with_name(f".{output.stem}.part{output.suffix}")
    cmd[-1] = str(tmp)  # last argument is the output path
    try:
        merger.run_merge_pass(cmd, expected, progress_cb, cancel_event)
        merger.verify_output(ffprobe, tmp, expected)
        tmp.replace(output)
    finally:
        tmp.unlink(missing_ok=True)
    return merger.probe(ffprobe, output)


def _main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="Sostituisce le sigle dentro un episodio (audio + video).")
    ap.add_argument("episodio", type=Path)
    ap.add_argument("--iniziale", type=Path, help="nuova sigla iniziale")
    ap.add_argument("--campione-iniziale", type=Path, help="vecchia sigla iniziale, da cercare nell'episodio")
    ap.add_argument("--finale", type=Path, help="nuova sigla finale")
    ap.add_argument("--campione-finale", type=Path, help="vecchia sigla finale, da cercare nell'episodio")
    ap.add_argument("-o", "--output", type=Path, required=True)
    ap.add_argument("--crf", type=int, default=16)
    ap.add_argument("--preset", default="slow")
    ap.add_argument("--solo-piano", action="store_true", help="mostra solo i punti di taglio, senza codificare")
    args = ap.parse_args(argv)

    ffmpeg, ffprobe = utils.find_tool("ffmpeg"), utils.find_tool("ffprobe")
    if not ffmpeg or not ffprobe:
        print("ffmpeg/ffprobe non trovati", file=sys.stderr)
        return 1
    ep = merger.probe(ffprobe, args.episodio)

    intro = outro = None
    if args.iniziale:
        intro = trova_sigla.cerca(args.episodio, args.campione_iniziale, "apertura", ffmpeg=ffmpeg, ffprobe=ffprobe)
        print("sigla iniziale vecchia:", intro)
        if intro.status != "trovata":
            return 2
    if args.finale:
        outro = trova_sigla.cerca(args.episodio, args.campione_finale, "chiusura", ffmpeg=ffmpeg, ffprobe=ffprobe)
        print("sigla finale vecchia:  ", outro)
        if outro.status != "trovata":
            return 2

    plan = plan_cuts(ffmpeg, args.episodio, ep.duration, intro, outro)
    print(f"Si tiene l'episodio da {plan.keep_start:.2f} s a {plan.keep_end:.2f} s "
          f"(silenzio per i primi {plan.mute_head:.2f} s: coda della vecchia musica)")
    if args.solo_piano:
        return 0

    settings = merger.MergeSettings(
        codec="h264", encoder="libx264", is_hardware=False, preset=args.preset,
        bitrate_mode="crf", crf=args.crf,
    )
    last = [-1]

    def progress(p: float):
        pct = int(p * 100)
        if pct != last[0] and pct % 5 == 0:
            last[0] = pct
            print(f"  {pct}%", file=sys.stderr, flush=True)

    out = replace_sigle(ffmpeg, ffprobe, args.episodio, args.iniziale, args.finale, plan, args.output,
                        settings, progress_cb=progress)
    print(f"Fatto: {args.output} ({out.duration:.2f} s)")
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
