"""Scheda "Modifica tracce": cambia ordine, traccia predefinita, nome, lingua e presenza delle tracce audio
e dei sottotitoli di un file, SENZA ricodificare (si riscrive solo il contenitore: pochi secondi, nessuna perdita).
Il risultato è sempre un file nuovo, l'originale non si tocca."""
from __future__ import annotations

import copy
from dataclasses import dataclass, field
from pathlib import Path

import merger

AUDIO, SUB = "audio", "sub"

# Rule actions
FIRST_DEFAULT, FIRST, DEFAULT, DELETE, NO_DEFAULT = "first_default", "first", "default", "delete", "no_default"
KEEP_ONLY, RENAME, FIX_LANGUAGE = "keep_only", "rename", "fix_language"
DELETE_FORCED, CLEAR_FORCED, SET_FORCED = "delete_forced", "clear_forced", "set_forced"
ACTION_LABELS = [
    (FIRST_DEFAULT, "Metti per prima e rendi predefinita"),
    (FIRST, "Metti per prima"),
    (DEFAULT, "Rendi predefinita"),
    (DELETE, "Elimina"),
    (KEEP_ONLY, "Tieni solo le lingue (elimina le altre)"),
    (NO_DEFAULT, "Nessuna predefinita"),
    (RENAME, "Rinomina in..."),
    (FIX_LANGUAGE, "Assegna la lingua a quelle senza lingua"),
    (SET_FORCED, "Segna come «forzata»"),
    (CLEAR_FORCED, "Togli il segno «forzata»"),
    (DELETE_FORCED, "Elimina quelle «forzate»"),
]
# what the "language" box means for each action
ACTIONS_WITHOUT_LANGUAGE = (NO_DEFAULT, CLEAR_FORCED, DELETE_FORCED)  # these act on all the tracks (or the language given)
ACTIONS_NEEDING_NAME = (RENAME,)
LANGUAGE_IS_LIST = (KEEP_ONLY,)  # "ita, eng"
LANGUAGE_IS_TARGET = (FIX_LANGUAGE,)  # the language to give
ANY_LANGUAGE = "*"
_NO_LANGUAGE = ("", "und", "unk", "mis", "zxx")

_LANG_ALIASES = {
    "it": "ita", "en": "eng", "ja": "jpn", "jp": "jpn", "fr": "fra", "fre": "fra", "de": "deu", "ger": "deu",
    "es": "spa", "pt": "por", "ru": "rus", "zh": "zho", "chi": "zho", "ko": "kor",
}
_CONTAINER_SUFFIXES = (".mkv", ".mp4", ".mov", ".m4v")  # the formats that store track flags/titles properly
_MP4_LIKE = (".mp4", ".mov", ".m4v")
_KNOWN_FLAGS = ("hearing_impaired", "visual_impaired", "comment", "original", "dub", "karaoke", "lyrics",
                "captions", "descriptions", "metadata", "clean_effects", "non_diegetic")


class TracceError(Exception):
    pass


def norm_lang(code: str | None) -> str:
    code = (code or "").strip().lower()
    return _LANG_ALIASES.get(code, code)


@dataclass
class Track:
    kind: str  # AUDIO | SUB
    src: int  # position among the source file's tracks of that kind (ffmpeg's a:N / s:N)
    codec: str = ""
    language: str = ""
    title: str = ""
    default: bool = False
    forced: bool = False
    channels: int = 0
    flags: tuple[str, ...] = ()  # other disposition flags worth keeping (hearing_impaired, comment, ...)

    def key(self) -> tuple:
        return (self.src, self.language, self.title, self.default, self.forced)


@dataclass
class FileTracks:
    path: Path
    duration: float = 0.0
    audio: list[Track] = field(default_factory=list)
    subs: list[Track] = field(default_factory=list)
    attachments: int = 0
    error: str | None = None

    def of(self, kind: str) -> list[Track]:
        return self.audio if kind == AUDIO else self.subs

    def signature(self) -> tuple:
        return (tuple(t.key() for t in self.audio), tuple(t.key() for t in self.subs))


@dataclass
class Rule:
    kind: str  # AUDIO | SUB
    action: str
    language: str = ANY_LANGUAGE  # KEEP_ONLY: "ita, eng"; FIX_LANGUAGE: the language to assign
    name: str = ""  # RENAME: the new track name (empty = remove the name)


def read_tracks(ffprobe: str, path: Path) -> FileTracks:
    ft = FileTracks(path)
    try:
        data = merger._run_ffprobe(ffprobe, path)
    except merger.MergeError as exc:
        ft.error = str(exc)
        return ft
    try:
        ft.duration = float((data.get("format") or {}).get("duration") or 0.0)
    except (TypeError, ValueError):
        ft.duration = 0.0
    for s in data.get("streams", []):
        kind = s.get("codec_type")
        if kind == "attachment":
            ft.attachments += 1
            continue
        if kind not in ("audio", "subtitle"):
            continue
        tags = s.get("tags") or {}
        disp = s.get("disposition") or {}
        track = Track(
            kind=AUDIO if kind == "audio" else SUB,
            src=len(ft.audio) if kind == "audio" else len(ft.subs),
            codec=s.get("codec_name", ""),
            language=norm_lang(tags.get("language")),
            title=tags.get("title") or "",
            default=bool(disp.get("default")),
            forced=bool(disp.get("forced")),
            channels=int(s.get("channels") or 0),
            flags=tuple(f for f in _KNOWN_FLAGS if disp.get(f)),
        )
        (ft.audio if kind == "audio" else ft.subs).append(track)
    if ft.duration <= 0:
        ft.error = f"{path.name}: durata non rilevabile."
    return ft


def describe(tracks: list[Track]) -> str:
    """"ita*, eng" - * marks the default track, (f) a forced one."""
    if not tracks:
        return "-"
    parts = []
    for t in tracks:
        text = t.language or "?"
        if t.title and not t.language:
            text = t.title[:12]
        parts.append(text + ("*" if t.default else "") + (" (f)" if t.forced else ""))
    return ", ".join(parts)


def apply_rules(ft: FileTracks, rules: list[Rule]) -> tuple[FileTracks, list[str]]:
    """The file's tracks after the rules, plus notes about rules that were skipped for safety."""
    out = copy.deepcopy(ft)
    notes: list[str] = []
    for rule in rules:
        lst = out.of(rule.kind)
        wanted = norm_lang(rule.language)
        if rule.action == KEEP_ONLY:
            keep = {norm_lang(x) for x in rule.language.replace(";", ",").split(",") if x.strip()}
            if not keep or ANY_LANGUAGE in keep:
                notes.append("«tieni solo le lingue»: nessuna lingua indicata")
                continue
            rest = [t for t in lst if t.language in keep]
            if not rest and rule.kind == AUDIO:
                notes.append("nessuna traccia audio nelle lingue da tenere: non elimino niente")
                continue
            lst[:] = rest
            continue
        if rule.action == FIX_LANGUAGE:
            if wanted in _NO_LANGUAGE or wanted == ANY_LANGUAGE:
                notes.append("«assegna la lingua»: nessuna lingua indicata")
                continue
            for t in lst:
                if t.language in _NO_LANGUAGE:
                    t.language = wanted
            continue
        if rule.action == RENAME:
            for t in lst:
                if wanted in ("", ANY_LANGUAGE) or t.language == wanted:
                    t.title = rule.name.strip()
            continue
        match = [t for t in lst if wanted in ("", ANY_LANGUAGE) or t.language == wanted]
        if rule.action == SET_FORCED:
            for t in match:
                t.forced = True
            continue
        if rule.action == CLEAR_FORCED:
            for t in match:
                t.forced = False
            continue
        if rule.action == DELETE_FORCED:
            gone = [t for t in match if t.forced]
            if len(gone) == len(lst) and rule.kind == AUDIO:
                notes.append("non elimino tutte le tracce audio")
                continue
            lst[:] = [t for t in lst if t not in gone]
            continue
        rest = [t for t in lst if t not in match]
        if rule.action in (FIRST, FIRST_DEFAULT) and match:
            lst[:] = match + rest
        if rule.action in (DEFAULT, FIRST_DEFAULT) and match:
            for t in lst:
                t.default = t is match[0]
        elif rule.action == DELETE and match:
            if len(match) == len(lst) and rule.kind == AUDIO:
                notes.append("non elimino tutte le tracce audio")
                continue
            lst[:] = rest
        elif rule.action == NO_DEFAULT:
            for t in lst:
                t.default = False
    # a file should keep one default audio track, whatever the rules did to it
    if out.audio and not any(t.default for t in out.audio):
        out.audio[0].default = True
    return out, notes


def target_suffix(source_suffix: str) -> str:
    """avi/flv/wmv/ts... can't store track flags and titles properly: those come out as .mkv."""
    return source_suffix if source_suffix.lower() in _CONTAINER_SUFFIXES else ".mkv"


def _sub_allowed(suffix: str, codec: str) -> bool:
    if suffix == ".mkv":
        return True
    return suffix in _MP4_LIKE and codec in merger._TEXT_SUBTITLE_CODECS


def _disposition(track: Track) -> str:
    flags = list(track.flags)
    if track.default:
        flags.append("default")
    if track.forced:
        flags.append("forced")
    return "+".join(flags) if flags else "0"


def build_command(ffmpeg: str, source: Path, plan: FileTracks, output: Path) -> tuple[list[str], list[str]]:
    """(ffmpeg command, notes). Everything is copied; only the audio/subtitle selection, order, flags,
    names and languages change."""
    suffix = output.suffix.lower()
    notes: list[str] = []
    subs = [t for t in plan.subs if _sub_allowed(suffix, t.codec)]
    if len(subs) != len(plan.subs):
        notes.append(f"{len(plan.subs) - len(subs)} sottotitoli non supportati dal formato {suffix}: saltati")
    # old containers (avi, mpg...) often have no usable timestamps on some streams: have ffmpeg generate them,
    # or the mkv muxer refuses the copy ("unknown timestamp")
    genpts = ["-fflags", "+genpts"] if Path(source).suffix.lower() in (".avi", ".mpg", ".mpeg", ".wmv", ".flv") else []
    cmd = [ffmpeg, "-y", "-hide_banner", "-loglevel", "error", *genpts, "-i", str(source), "-map", "0:v?"]
    for t in plan.audio:
        cmd += ["-map", f"0:a:{t.src}"]
    for t in subs:
        cmd += ["-map", f"0:s:{t.src}"]
    if suffix == ".mkv" and plan.attachments:
        cmd += ["-map", "0:t?"]  # attachment streams have to come last
    cmd += ["-map_chapters", "0", "-map_metadata", "0", "-c", "copy"]
    if suffix in _MP4_LIKE and subs:
        cmd += ["-c:s", "mov_text"]
    for n, t in enumerate(plan.audio):
        cmd += [f"-disposition:a:{n}", _disposition(t)]
        cmd += [f"-metadata:s:a:{n}", f"language={t.language or 'und'}", f"-metadata:s:a:{n}", f"title={t.title}"]
        if suffix in _MP4_LIKE and t.title:
            cmd += [f"-metadata:s:a:{n}", f"handler_name={t.title}"]
    for n, t in enumerate(subs):
        cmd += [f"-disposition:s:{n}", _disposition(t)]
        cmd += [f"-metadata:s:s:{n}", f"language={t.language or 'und'}", f"-metadata:s:s:{n}", f"title={t.title}"]
        if suffix in _MP4_LIKE and t.title:
            cmd += [f"-metadata:s:s:{n}", f"handler_name={t.title}"]
    cmd += ["-progress", "pipe:1", "-nostats", str(output)]
    return cmd, notes


def remux(
    ffmpeg: str, ffprobe: str, source: FileTracks, plan: FileTracks, output_dir: Path,
    progress_cb=None, cancel_event=None,
) -> merger.MergeResult:
    """Writes the edited copy of one file into output_dir (an existing file there is left alone)."""
    suffix = target_suffix(source.path.suffix)
    output = output_dir / (source.path.stem + suffix)
    try:
        if output.exists():
            return merger.MergeResult(output_path=output, success=True, skipped=True)
        if source.signature() == plan.signature() and suffix == source.path.suffix.lower():
            return merger.MergeResult(output_path=output, success=True, skipped=True, note="nessuna modifica")
        if not plan.audio and source.audio:
            raise TracceError("Il file non avrebbe più nessuna traccia audio.")
        output_dir.mkdir(parents=True, exist_ok=True)
        cmd, notes = build_command(ffmpeg, source.path, plan, output)
        tmp = output.with_name(f".{output.stem}.part{output.suffix}")
        cmd[-1] = str(tmp)
        try:
            merger.run_merge_pass(cmd, source.duration, progress_cb, cancel_event)
            merger.verify_output(ffprobe, tmp, source.duration)
            check = read_tracks(ffprobe, tmp)
            expected_subs = len([t for t in plan.subs if _sub_allowed(suffix, t.codec)])
            if len(check.audio) != len(plan.audio) or len(check.subs) != expected_subs:
                raise TracceError(
                    f"Controllo finale fallito: tracce audio {len(check.audio)}/{len(plan.audio)}, "
                    f"sottotitoli {len(check.subs)}/{expected_subs}.")
            tmp.replace(output)
        finally:
            tmp.unlink(missing_ok=True)
        if suffix != source.path.suffix.lower():
            notes.append(f"salvato come {suffix}: il formato {source.path.suffix} non conserva bene le tracce")
        return merger.MergeResult(output_path=output, success=True, note="; ".join(notes) or None)
    except merger.Cancelled:
        return merger.MergeResult(output_path=output, success=False, error="Annullato")
    except (merger.MergeError, TracceError) as exc:
        return merger.MergeResult(output_path=output, success=False, error=str(exc))
