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
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

import encoding
import merger
import profili
import trova_sigla
import utils
from utils import CREATE_NO_WINDOW

_BLACK_RE = re.compile(r"black_start:([\d.]+) black_end:([\d.]+)")
_SCENE_RE = re.compile(r"pts_time:([\d.]+)\s*\n.*?lavfi\.scene_score=([\d.]+)")


class SostituisciError(Exception):
    pass


@dataclass(frozen=True, eq=False)
class Profile:
    """How the old sigle of a series are built, when that is not the usual "opening, then the episode".
    «Standard» is built in; the others are made by the user (see profili.py)."""
    key: str
    name: str
    description: str = ""
    title_card_in_intro: bool = False  # the episode's title card is shown INSIDE the old opening: it is kept, silent, after the new one
    end_lookback: float = 0.0  # the old closing's music starts later than its picture: look this many seconds back for the picture's cut
    edge_after: float = 0.3  # the black that ends the old opening may begin this long after the music has ended
    settings: dict = field(default_factory=dict)  # what the tab looked like when the profile was saved (new sigle, quality...)
    builtin: bool = False

    def to_dict(self) -> dict:
        return {"nome": self.name, "descrizione": self.description, "scheda_nella_sigla": self.title_card_in_intro,
                "indietro_titoli_di_coda": self.end_lookback, "tolleranza_nero": self.edge_after,
                "impostazioni": self.settings}

    @staticmethod
    def from_dict(key: str, d: dict) -> "Profile":
        def num(name: str, default: float) -> float:
            try:
                return max(0.0, float(d.get(name, default)))
            except (TypeError, ValueError):
                return default
        settings = d.get("impostazioni")
        return Profile(key, str(d.get("nome", key)), str(d.get("descrizione", "")), bool(d.get("scheda_nella_sigla", False)),
                       num("indietro_titoli_di_coda", 0.0), num("tolleranza_nero", 0.3),
                       settings if isinstance(settings, dict) else {})


STANDARD = Profile("standard", "Standard", "Una sigla iniziale e/o finale da sostituire; il resto dell'episodio resta com'è.",
                   builtin=True)
PROFILES = {"standard": STANDARD}  # the built-in one only


def load_profiles() -> dict[str, Profile]:
    """«Standard» first, then the user's profiles by name."""
    found = dict(PROFILES)
    for key, data in profili.all_dicts("sostituisci").items():
        found[key] = Profile.from_dict(key, data)
    return found


@dataclass
class CutPlan:
    """Which part of the episode survives: [keep_start, keep_end] in the episode's own time."""
    keep_start: float  # where the old opening's animation stops (what follows, e.g. an episode-title card, stays)
    keep_end: float  # where the old closing credits begin
    mute_head: float  # seconds, from keep_start, whose sound is the old opening's music tail: silenced
    old_intro: trova_sigla.Detection | None
    old_outro: trova_sigla.Detection | None
    # how each cut was decided: "titolo" / "nero" (a black frame), "stacco" (a hard picture cut),
    # "audio" (only the sound's boundary: least precise), "manuale", or "" when that sigla is left alone
    start_kind: str = ""
    end_kind: str = ""
    card: tuple[float, float] | None = None  # the title card shown inside the old opening (episode time), when it is kept


def black_intervals(ffmpeg: str, path: Path, start: float, duration: float) -> list[tuple[float, float]]:
    """Stretches of fully black picture inside [start, start + duration] (times in the file)."""
    start = max(start, 0.0)
    proc = subprocess.run(
        [ffmpeg, "-hide_banner", "-ss", f"{start:.3f}", "-t", f"{duration:.3f}", "-i", str(path),
         "-an", "-vf", "blackdetect=d=0.1:pix_th=0.1", "-f", "null", "-"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", creationflags=CREATE_NO_WINDOW,
    )
    return [(float(a) + start, float(b) + start) for a, b in _BLACK_RE.findall(proc.stderr)]


def scene_cut_near(ffmpeg: str, path: Path, t: float, radius: float = 1.0, threshold: float = 0.25) -> float | None:
    """Time of the strongest hard cut of the picture within `radius` seconds of `t`, or None. A sigla
    that is glued straight onto the episode ends exactly on such a cut: the audio detection only
    knows the boundary to a few hundredths of a second, the cut gives the exact frame."""
    start = max(t - radius, 0.0)
    proc = subprocess.run(
        [ffmpeg, "-hide_banner", "-ss", f"{start:.3f}", "-t", f"{2 * radius:.3f}", "-i", str(path), "-an",
         "-vf", f"select='gt(scene,{threshold})',metadata=print", "-f", "null", "-"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", creationflags=CREATE_NO_WINDOW,
    )
    cuts = [(float(a) + start, float(b)) for a, b in _SCENE_RE.findall(proc.stderr)]
    return max(cuts, key=lambda c: c[1])[0] if cuts else None


def scene_cuts(ffmpeg: str, path: Path, start: float, length: float, threshold: float) -> list[tuple[float, float]]:
    """Every hard cut of the picture in [start, start + length]: (time, strength 0..1)."""
    start = max(start, 0.0)
    proc = subprocess.run(
        [ffmpeg, "-hide_banner", "-ss", f"{start:.3f}", "-t", f"{length:.3f}", "-i", str(path), "-an", "-sn",
         "-vf", f"select='gt(scene,{threshold})',metadata=print", "-f", "null", "-"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", creationflags=CREATE_NO_WINDOW,
    )
    return [(float(a) + start, float(b)) for a, b in _SCENE_RE.findall(proc.stderr)]


def find_title_card(ffmpeg: str, episode: Path, intro: trova_sigla.Detection) -> tuple[float, float] | None:
    """The episode's title card inside the old opening: a card on a salmon-red background (the same frame in every
    episode, only the lettering changes) that stays on screen a few seconds. Returns its first and last picture time,
    put on the hard cuts that bracket it."""
    start = max(intro.start, 0.0)
    length = min(max(intro.end - start, 10.0), 120.0)
    raw = subprocess.run(
        [ffmpeg, "-v", "error", "-nostdin", "-ss", f"{start:.3f}", "-t", f"{length:.3f}", "-i", str(episode), "-an", "-sn",
         "-vf", "fps=25,scale=64:48,format=rgb24", "-f", "rawvideo", "-"],
        capture_output=True, creationflags=CREATE_NO_WINDOW).stdout
    n = len(raw) // (64 * 48 * 3)
    if n < 50:
        return None
    fr = np.frombuffer(raw[: n * 64 * 48 * 3], dtype=np.uint8).reshape(n, 64 * 48, 3).astype(int)
    r, g, b = fr[:, :, 0], fr[:, :, 1], fr[:, :, 2]
    red = ((r > 150) & (g < 110) & (b < 110) & (r - g > 70)).mean(axis=1) > 0.45
    best, i = None, 0
    while i < n:
        if red[i]:
            j = i
            while j + 1 < n and red[j + 1]:
                j += 1
            if (j + 1 - i) >= 50 and (best is None or (j - i) > (best[1] - best[0])):  # at least 2 s: a real card
                best = (i, j)
            i = j + 1
        else:
            i += 1
    if best is None:
        return None
    first, last = start + best[0] / 25, start + (best[1] + 1) / 25
    a = scene_cut_near(ffmpeg, episode, first, 0.3) or first
    z = scene_cut_near(ffmpeg, episode, last, 0.3) or last
    return (round(a, 3), round(z, 3)) if z - a >= 2.0 else None


def closing_cut_before(ffmpeg: str, episode: Path, t: float, max_back: float) -> float | None:
    """Some old closings show their credits several seconds before their music begins. The picture then changes with
    one strong hard cut into the credits, followed by a stretch without other cuts that lasts until the music starts.
    Returns that cut (time), looking back at most `max_back` seconds from `t`, else None."""
    cuts = scene_cuts(ffmpeg, episode, t - max_back, max_back + 0.5, 0.25)
    for time_c, strength in sorted(cuts):
        if strength < 0.7 or time_c > t - 0.5:
            continue
        if not [c for c in cuts if time_c + 0.5 < c[0] < t - 0.3]:
            return time_c
    return None


def _inset(start: float, end: float, cap: float) -> float:
    """A cut a little way into a black stretch (never past its middle): the first frames of a
    "black" are often still the faint tail of a fade-out, and must not end up in the output."""
    return start + min(cap, (end - start) / 2)


def plan_cuts(
    ffmpeg: str, episode: Path, duration: float,
    intro: trova_sigla.Detection | None, outro: trova_sigla.Detection | None,
    keep_card: bool = True, profile: Profile = STANDARD,
) -> CutPlan:
    """Turns the audio detections into picture cut points, snapping to the black frames the sigle
    are bracketed with. After the opening there is often a title card (episode name) that is
    shown while the music still plays: it sits between two black stretches, the second one close
    to where the music ends. With `keep_card` the cut is made at the black BEFORE the card, so the
    card stays (its sound silenced until the old music ends); without it the cut is made at the
    black after the card, so the card goes away with the sigla. The closing is cut inside the
    black that precedes the credits."""
    keep_start, mute_head = 0.0, 0.0
    start_kind = end_kind = ""
    if intro is not None:
        blacks = black_intervals(ffmpeg, episode, max(intro.end - 15.0, 0.0), 23.0)
        card = None
        for first, second in zip(blacks, blacks[1:]):
            if 1.2 <= second[0] - first[1] <= 6.0 and abs(second[0] - intro.end) <= 3.0:
                card = (first, second)  # keeps the last such pair
        # a black that starts shortly before the music ends is the one bracketing the sigla
        # (a black deep inside the sigla is just part of its animation)
        edge = [b for b in blacks if intro.end - 3.0 <= b[0] <= intro.end + profile.edge_after]
        if card is not None:
            first, second = card
            keep_start = _inset(*first, 0.1) if keep_card else _inset(*second, 0.3)
            mute_head = max(intro.end - keep_start, 0.0)
            start_kind = "titolo" if keep_card else "nero"
        elif edge:
            keep_start = _inset(*edge[-1], 0.1)
            mute_head = max(intro.end - keep_start, 0.0)
            start_kind = "nero"
        else:
            # no black: the sigla is glued straight onto the episode. Snap to the picture's hard cut
            # next to where the music ends, else trust the audio boundary.
            cut = scene_cut_near(ffmpeg, episode, intro.end)
            keep_start = cut if cut is not None else intro.end
            start_kind = "stacco" if cut is not None else "audio"
    keep_end = duration
    if outro is not None:
        keep_end = outro.start
        end_kind = "audio"
        blacks = black_intervals(ffmpeg, episode, outro.start - 8.0, 10.0)
        near = [b for b in blacks if b[0] <= outro.start + 0.5 and b[1] >= outro.start - 3.0]
        if near:
            keep_end = _inset(*near[-1], 0.25)
            end_kind = "nero"
        else:
            cut = scene_cut_near(ffmpeg, episode, outro.start)
            if cut is not None:
                keep_end = cut
                end_kind = "stacco"
            elif profile.end_lookback > 0:
                cut = closing_cut_before(ffmpeg, episode, outro.start, profile.end_lookback)
                if cut is not None:
                    keep_end = cut
                    end_kind = "immagine"
    if keep_end <= keep_start + 60:
        raise SostituisciError("I punti di taglio trovati non sono coerenti: controlla le sigle rilevate.")
    card = find_title_card(ffmpeg, episode, intro) if (intro is not None and profile.title_card_in_intro) else None
    return CutPlan(keep_start, keep_end, mute_head, intro, outro, start_kind, end_kind, card)


def replace_sigle(
    ffmpeg: str, ffprobe: str, episode: Path, new_intro: Path | None, new_outro: Path | None,
    plan: CutPlan, output: Path, settings: merger.MergeSettings,
    progress_cb=None, cancel_event=None, episode_interlaced: bool | None = None,
) -> merger.ProbeResult:
    """Builds and runs the single re-encode: new opening + kept part of the episode + new closing."""
    ep = merger.probe(ffprobe, episode)
    if episode_interlaced is not None:  # decided by the caller (looking at the picture), whatever the file's flag says
        ep.interlaced = episode_interlaced
    segments: list[tuple[Path, merger.ProbeResult]] = []
    if new_intro is not None:
        segments.append((new_intro, merger.probe(ffprobe, new_intro)))
    # the title card that was inside the old opening goes right after the new one (picture only, no sound)
    card_idx = None
    if new_intro is not None and plan.card is not None:
        card_idx = len(segments)
        segments.append((episode, ep))
    episode_idx = len(segments)
    segments.append((episode, ep))
    if new_outro is not None:
        segments.append((new_outro, merger.probe(ffprobe, new_outro)))

    trims = {episode_idx: (plan.keep_start, plan.keep_end)}
    mutes = {episode_idx: plan.mute_head} if plan.mute_head > 0 else {}
    card_len = 0.0
    if card_idx is not None:
        trims[card_idx] = plan.card
        card_len = plan.card[1] - plan.card[0]
        mutes[card_idx] = card_len + 1.0
    expected = (sum(p.duration for i, (_, p) in enumerate(segments) if i not in (episode_idx, card_idx))
                + card_len + (plan.keep_end - plan.keep_start))
    tmp = output.with_name(f".{output.stem}.part{output.suffix}")
    try:
        merger.run_encode(ffmpeg, segments, episode_idx, tmp, settings, expected, progress_cb, cancel_event,
                          trims=trims, mutes=mutes or None)
        merger.verify_output(ffprobe, tmp, expected)
        tmp.replace(output)
    finally:
        tmp.unlink(missing_ok=True)
    return merger.probe(ffprobe, output)


# ----------------------------------------------------------------------------------------
# Analysis of a whole folder, and the per-episode work, as used by the "Sostituisci sigla" tab

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
    profile: Profile = STANDARD

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
        if self.note:
            return "Da verificare"
        return "Trovata"

    @property
    def note(self) -> str:
        """Why a found sigla still deserves a look (empty when nothing)."""
        if self.plan is None:
            return ""
        if (self.want_intro and self.plan.start_kind == "audio") or (self.want_outro and self.plan.end_kind == "audio"):
            return "taglio deciso solo dall'audio: controlla l'anteprima"
        if self.want_intro and self.profile.title_card_in_intro and self.plan.card is None:
            return "scheda del titolo non trovata dentro la vecchia sigla"
        return ""

    @property
    def ready(self) -> bool:
        return self.plan is not None and self.status in ("Trovata", "Da verificare")


def analizza(
    episodes: list[Path], intro_cfg, outro_cfg, ffmpeg: str, ffprobe: str, progress=None, cancelled=None,
    keep_card: bool = True, profile: Profile = STANDARD,
) -> list[EpisodeAnalysis]:
    """Finds the old sigle in every episode and decides the cut points. `intro_cfg`/`outro_cfg`:
    None (leave that sigla alone), "impara" (learn it from the episodes) or the Path of a sample
    file. progress(fraction 0..1, text) / cancelled() are optional."""
    def check():
        if cancelled is not None and cancelled():
            raise Annullato()

    results = {ep: EpisodeAnalysis(ep, want_intro=intro_cfg is not None, want_outro=outro_cfg is not None, profile=profile)
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
                                     res.intro if res.want_intro else None, res.outro if res.want_outro else None,
                                     keep_card=keep_card, profile=profile)
        except (merger.MergeError, SostituisciError) as exc:
            res.error = str(exc)
    if progress:
        progress(1.0, "Analisi completata")
    return [results[ep] for ep in episodes]


def elabora_episodio(
    ffmpeg: str, ffprobe: str, ana: EpisodeAnalysis, new_intro: Path | None, new_outro: Path | None,
    output_dir: Path, opts: "encoding.EncodingOptions", hw_encoders: dict | None = None, progress_cb=None, cancel_event=None,
) -> merger.MergeResult:
    """Replaces the sigle of one analysed episode, encoding as `opts` says (by default the episode's own codec,
    H.265 when that isn't H.264/H.265/AV1, and its container where that can hold it)."""
    suffix = ana.episode.suffix
    output = output_dir / (ana.episode.stem + suffix)
    try:
        if ana.plan is None:
            raise SostituisciError("Episodio non analizzato.")
        info = merger.probe(ffprobe, ana.episode)
        settings, notes = encoding.build_settings(opts, info, hw_encoders, ffmpeg)
        suffix, changed = merger.pick_container(ana.episode.suffix, settings.codec, opts.container)
        output = output_dir / (ana.episode.stem + suffix)
        if output.exists():
            return merger.MergeResult(output_path=output, success=True, skipped=True)
        output_dir.mkdir(parents=True, exist_ok=True)
        interlaced = encoding.resolve_interlaced(opts, ffmpeg, ana.episode, info)
        replace_sigle(ffmpeg, ffprobe, ana.episode, new_intro, new_outro, ana.plan, output, settings,
                      progress_cb=progress_cb, cancel_event=cancel_event, episode_interlaced=interlaced)
        if changed:
            notes.append(f"salvato come {suffix}: il formato {ana.episode.suffix} non supporta questo codec")
        if interlaced and settings.deinterlace:
            notes.append("video interlacciato: deinterlacciato")
        return merger.MergeResult(output_path=output, success=True, note="; ".join(notes) or None)
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
        segs = [(new_intro, ni)]
        trims = {0: (max(ni.duration - 6.0, 0.0), ni.duration)}
        mutes: dict[int, float] = {}
        card_len = 0.0
        if plan.card is not None:  # the title card kept after the new opening, silent
            segs.append((ana.episode, ep))
            trims[1] = plan.card
            card_len = plan.card[1] - plan.card[0]
            mutes[1] = card_len + 1.0
        segs.append((ana.episode, ep))
        ep_idx = len(segs) - 1
        trims[ep_idx] = (plan.keep_start, plan.keep_start + 9.0)
        if plan.mute_head > 0:
            mutes[ep_idx] = plan.mute_head
        cmd = merger.build_command(ffmpeg, segs, ep_idx, out, settings, trims=trims, mutes=mutes or None)
        out.unlink(missing_ok=True)
        merger.run_merge_pass(cmd, 15.0 + card_len, None, None)
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
