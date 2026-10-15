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


# ----------------------------------------------------------------------------------------
# Analysis of a whole folder, and the per-episode work, as used by the "Sostituisci sigla" tab

# Quality levels of the tab (CRF of H.264; the other codecs get a nearby value that measured
# about the same picture fidelity on a 40 s cartoon excerpt: H.265 needs one point less, AV1 one more).
QUALITY_LEVELS = [("Massima (file molto grandi)", 14), ("Alta (consigliata)", 16), ("Bilanciata", 20), ("Compatta", 23)]
_CRF_OFFSET = {"h264": 0, "h265": -1, "av1": 1}
CONFIDENCE_OK = 70  # below this a found sigla is shown as "Da verificare"


class Annullato(Exception):
    pass


@dataclass
class EpisodeAnalysis:
    episode: Path
    want_intro: bool = False
    want_outro: bool = False
    duration: float = 0.0
    intro: trova_sigla.Detection | None = None
    outro: trova_sigla.Detection | None = None
    plan: CutPlan | None = None
    error: str | None = None

    def _sides(self) -> list[trova_sigla.Detection | None]:
        return ([self.intro] if self.want_intro else []) + ([self.outro] if self.want_outro else [])

    @property
    def confidence(self) -> int:
        dets = self._sides()
        if any(d is None or d.status == "non_trovata" for d in dets):
            return 0
        return min((d.confidence for d in dets), default=0)

    @property
    def status(self) -> str:
        if self.error:
            return "Errore"
        dets = self._sides()
        if any(d is None or d.status == "non_trovata" for d in dets):
            return "Non trovata"
        if any(d.status == "piu_volte" or d.confidence < CONFIDENCE_OK for d in dets):
            return "Da verificare"
        return "Trovata"

    @property
    def ready(self) -> bool:
        return self.plan is not None and self.status in ("Trovata", "Da verificare")


def analizza(
    episodes: list[Path], intro_cfg, outro_cfg, ffmpeg: str, ffprobe: str, progress=None, cancelled=None,
) -> list[EpisodeAnalysis]:
    """Finds the old sigle in every episode and decides the cut points. `intro_cfg`/`outro_cfg`:
    None (leave that sigla alone), "impara" (learn it from the episodes) or the Path of a sample
    file. progress(fraction 0..1, text) / cancelled() are optional."""
    def check():
        if cancelled is not None and cancelled():
            raise Annullato()

    results = {ep: EpisodeAnalysis(ep, want_intro=intro_cfg is not None, want_outro=outro_cfg is not None)
               for ep in episodes}
    stages = [(name, zone, cfg) for name, zone, cfg in
              (("iniziale", "apertura", intro_cfg), ("finale", "chiusura", outro_cfg)) if cfg is not None]
    n_stages = len(stages) + 1  # + choosing the cut points

    def report(stage: int, done: float, total: float, text: str):
        check()
        if progress:
            progress((stage + (done / total if total else 0.0)) / n_stages, text)

    for k, (name, zone, cfg) in enumerate(stages):
        attr = "intro" if name == "iniziale" else "outro"
        if cfg == "impara":
            try:
                learned = trova_sigla.impara(
                    episodes, zone, 6.0, 0, ffmpeg, ffprobe,
                    progress=lambda d, t, what, k=k, name=name: report(k, d, t, f"Sigla {name}: {what}"),
                )
            except trova_sigla.SiglaError as exc:
                raise SostituisciError(f"Sigla {name}: {exc}") from exc
            for ep, det in learned.detections.items():
                setattr(results[ep], attr, det)
        else:
            for i, ep in enumerate(episodes):
                report(k, i, len(episodes), f"Sigla {name}: cerco in {ep.name}")
                try:
                    setattr(results[ep], attr, trova_sigla.cerca(ep, cfg, zone, ffmpeg=ffmpeg, ffprobe=ffprobe))
                except trova_sigla.SiglaError as exc:
                    results[ep].error = str(exc)

    for i, ep in enumerate(episodes):
        report(len(stages), i, len(episodes), f"Punti di taglio: {ep.name}")
        res = results[ep]
        if res.error:
            continue
        try:
            res.duration = merger.probe(ffprobe, ep).duration
            if res.status in ("Trovata", "Da verificare"):
                res.plan = plan_cuts(ffmpeg, ep, res.duration,
                                     res.intro if res.want_intro else None, res.outro if res.want_outro else None)
        except (merger.MergeError, SostituisciError) as exc:
            res.error = str(exc)
    if progress:
        progress(1.0, "Analisi completata")
    return [results[ep] for ep in episodes]


def elabora_episodio(
    ffmpeg: str, ffprobe: str, ana: EpisodeAnalysis, new_intro: Path | None, new_outro: Path | None,
    output_dir: Path, quality: int, preset: str, audio_mode: str, progress_cb=None, cancel_event=None,
) -> merger.MergeResult:
    """Replaces the sigle of one analysed episode. The video keeps the episode's own codec
    (H.265 when that isn't H.264/H.265/AV1) and its container where that can hold it."""
    suffix = ana.episode.suffix
    output = output_dir / (ana.episode.stem + suffix)
    try:
        if ana.plan is None:
            raise SostituisciError("Episodio non analizzato.")
        info = merger.probe(ffprobe, ana.episode)
        codec = merger.app_codec_for(info.video_codec) or "h265"
        suffix, changed = merger.pick_container(ana.episode.suffix, codec)
        output = output_dir / (ana.episode.stem + suffix)
        if output.exists():
            return merger.MergeResult(output_path=output, success=True, skipped=True)
        output_dir.mkdir(parents=True, exist_ok=True)
        settings = merger.MergeSettings(
            codec=codec, encoder=utils.CODEC_ENCODERS[codec][0], is_hardware=False, preset=preset,
            bitrate_mode="crf", crf=quality + _CRF_OFFSET[codec], audio_mode=audio_mode,
        )
        replace_sigle(ffmpeg, ffprobe, ana.episode, new_intro, new_outro, ana.plan, output, settings,
                      progress_cb=progress_cb, cancel_event=cancel_event)
        note = f"salvato come {suffix}: il formato {ana.episode.suffix} non supporta questo codec" if changed else None
        return merger.MergeResult(output_path=output, success=True, note=note)
    except merger.Cancelled:
        return merger.MergeResult(output_path=output, success=False, error="Annullato")
    except (merger.MergeError, SostituisciError) as exc:
        return merger.MergeResult(output_path=output, success=False, error=str(exc))


def anteprima_giunzioni(
    ffmpeg: str, ffprobe: str, ana: EpisodeAnalysis, new_intro: Path | None, new_outro: Path | None,
    target_dir: Path,
) -> list[Path]:
    """Two short clips (about 15 s each) around the joins: end of the new opening + start of what is
    kept, and end of what is kept + start of the new closing. Quick low-quality encodes."""
    if ana.plan is None:
        raise SostituisciError("Episodio non analizzato.")
    plan = ana.plan
    target_dir.mkdir(parents=True, exist_ok=True)
    settings = merger.MergeSettings(codec="h264", encoder="libx264", is_hardware=False, preset="veryfast",
                                    bitrate_mode="crf", crf=24, audio_mode="aac_stereo")
    ep = merger.probe(ffprobe, ana.episode)
    clips: list[Path] = []
    if new_intro is not None:
        ni = merger.probe(ffprobe, new_intro)
        out = target_dir / f"{ana.episode.stem} - giunzione iniziale.mp4"
        cmd = merger.build_command(
            ffmpeg, [(new_intro, ni), (ana.episode, ep)], 1, out, settings,
            trims={0: (max(ni.duration - 6.0, 0.0), ni.duration), 1: (plan.keep_start, plan.keep_start + 9.0)},
            mutes={1: plan.mute_head} if plan.mute_head > 0 else None,
        )
        out.unlink(missing_ok=True)
        merger.run_merge_pass(cmd, 15.0, None, None)
        clips.append(out)
    if new_outro is not None:
        no = merger.probe(ffprobe, new_outro)
        out = target_dir / f"{ana.episode.stem} - giunzione finale.mp4"
        cmd = merger.build_command(
            ffmpeg, [(ana.episode, ep), (new_outro, no)], 0, out, settings,
            trims={0: (max(plan.keep_end - 9.0, 0.0), plan.keep_end), 1: (0.0, 6.0)},
        )
        out.unlink(missing_ok=True)
        merger.run_merge_pass(cmd, 15.0, None, None)
        clips.append(out)
    return clips


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
