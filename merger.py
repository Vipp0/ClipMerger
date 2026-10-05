"""Core video-merging logic: probing, ffmpeg command construction, execution with
progress reporting, and output integrity verification.

Design: the episode drives the target resolution/fps/audio-track-count. The intro and
outro clips are scaled/resampled to match each episode individually (never the other
way around).
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import threading
from dataclasses import dataclass, field
from fractions import Fraction
from functools import lru_cache
from pathlib import Path
from typing import Callable

from utils import CREATE_NO_WINDOW, CODEC_ENCODERS

TARGET_SAMPLE_RATE = 48000
STEREO_AAC_KBPS = 192
DURATION_TOLERANCE = 0.02  # 2%
TWO_PASS_ENCODERS = ("libx264", "libx265", "libsvtav1")  # software encoders that support -pass
MP4_LIKE_SUFFIXES = (".mp4", ".mov", ".m4v")  # these store track names in handler_name, not title
TEN_BIT_ENCODERS = ("libx265", "libsvtav1")  # software encoders whose 10-bit mode is used for 10-bit sources
# SVT-AV1 takes a number (-2..13) for -preset, not the fast/medium/slow words the others accept.
_SVTAV1_PRESETS = {"fast": "8", "medium": "6", "slow": "4"}
# Video codec -> containers that accept it, only used to pick a safe container when the
# user asks for "same as the original" and the source's own extension can't hold the output.
_CONTAINERS_FOR_CODEC = {
    "h264": {".mp4", ".mkv", ".mov", ".avi", ".ts", ".m2ts", ".flv", ".wmv"},
    "h265": {".mp4", ".mkv", ".mov", ".avi", ".ts", ".m2ts", ".flv"},
    "av1": {".mp4", ".mkv", ".avi", ".ts", ".m2ts", ".flv", ".wmv"},
}
_TEXT_SUBTITLE_CODECS = {"subrip", "srt", "ass", "ssa", "mov_text", "webvtt", "text"}


class MergeError(Exception):
    pass


class Cancelled(Exception):
    pass


@dataclass
class AudioStream:
    index: int
    language: str | None
    title: str | None = None
    default: bool = False
    codec: str = ""
    channels: int = 0
    channel_layout: str | None = None
    sample_rate: int = 0
    bits: int = 0  # bit depth (0 = unknown)
    bitrate: int | None = None  # bits/s


@dataclass
class SubtitleStream:
    index: int
    codec: str
    language: str | None
    title: str | None = None


@dataclass
class Chapter:
    start: float  # seconds
    end: float
    title: str | None


@dataclass
class Cover:
    pos: int  # ffmpeg "v:N" position of this cover-art stream in its source file
    codec: str
    filename: str | None


@dataclass
class CoverFile:
    """A cover image extracted to disk so it can be re-attached to an mkv as a real attachment."""
    path: Path
    mimetype: str
    filename: str


@dataclass
class ProbeResult:
    duration: float
    width: int
    height: int
    fps: float
    video_bitrate: int | None
    audio: list[AudioStream] = field(default_factory=list)
    subtitles: list[SubtitleStream] = field(default_factory=list)
    sar: Fraction = Fraction(1)  # pixel (sample) aspect ratio; != 1 means anamorphic
    chapters: list[Chapter] = field(default_factory=list)
    video_index: int = 0  # position of the real video among the file's video-type streams
    covers: list[Cover] = field(default_factory=list)  # embedded cover art (shows up as a "video")
    attachment_count: int = 0  # mkv attachments proper (e.g. subtitle fonts)
    interlaced: bool = False  # field_order says the video is interlaced
    bit_depth: int = 8


@dataclass
class MergeSettings:
    codec: str  # "h264" | "h265" | "av1"
    encoder: str  # actual ffmpeg encoder name (software or hardware), from utils.pick_encoder
    is_hardware: bool
    preset: str  # "fast" | "medium" | "slow"
    bitrate_mode: str  # "crf" | "cbr" | "original"
    crf: int = 20
    cbr_kbps: int = 2000
    tune_animation: bool = False  # -tune animation; only meaningful for libx264/libx265
    two_pass: bool = False  # average-bitrate 2-pass; only for software encoders + cbr/original
    audio_mode: str = "original"  # "original" | "aac_keep" | "aac_stereo" | "flac"
    deinterlace: bool = True  # deinterlace sources flagged as interlaced (the flag can't survive re-encoding)
    keep_10bit: bool = True  # keep 10-bit sources 10-bit (software H.265/AV1 only)


@dataclass
class MergeResult:
    output_path: Path
    success: bool
    skipped: bool = False
    error: str | None = None
    note: str | None = None  # something worth telling the user about a successful merge


def _run_ffprobe(ffprobe_path: str, path: Path) -> dict:
    args = [
        ffprobe_path, "-v", "error", "-print_format", "json",
        "-show_format", "-show_streams", "-show_chapters", str(path),
    ]
    try:
        proc = subprocess.run(
            args, capture_output=True, text=True, timeout=30,
            # ffprobe's JSON is UTF-8; the default Windows codec would garble accents
            # in track titles ("è" -> "Ã¨"), and they're passed back to ffmpeg later.
            encoding="utf-8", errors="replace",
            creationflags=CREATE_NO_WINDOW,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        raise MergeError(f"Impossibile analizzare {path.name}: {exc}") from exc
    if proc.returncode != 0:
        raise MergeError(f"ffprobe ha fallito su {path.name}: {proc.stderr.strip()}")
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise MergeError(f"Output ffprobe non valido per {path.name}: {exc}") from exc


def _parse_fps(rate_str: str) -> float:
    if not rate_str or rate_str == "0/0":
        return 25.0
    if "/" in rate_str:
        num, den = rate_str.split("/")
        den = float(den)
        return float(num) / den if den else 25.0
    return float(rate_str)


def _parse_sar(sar_str: str | None) -> Fraction:
    """ffprobe reports e.g. "64:45", or "N/A" / "0:1" when unspecified (= square pixels)."""
    if not sar_str or ":" not in sar_str:
        return Fraction(1)
    try:
        num, den = (int(x) for x in sar_str.split(":"))
    except ValueError:
        return Fraction(1)
    return Fraction(num, den) if num > 0 and den > 0 else Fraction(1)


def _to_int(value) -> int | None:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return None
    return n if n > 0 else None


def _rotation_degrees(video_stream: dict) -> int:
    """Display rotation (0/90/180/270) from the display-matrix side data or the legacy
    "rotate" tag. ffmpeg auto-rotates the picture, so size computations must follow."""
    for side in video_stream.get("side_data_list") or []:
        if "rotation" in side:
            try:
                return int(round(float(side["rotation"]))) % 360
            except (TypeError, ValueError):
                pass
    try:
        return int((video_stream.get("tags") or {}).get("rotate", 0)) % 360
    except (TypeError, ValueError):
        return 0


def _bit_depth(video_stream: dict) -> int:
    m = re.search(r"(\d+)(?:le|be)$", video_stream.get("pix_fmt") or "")
    return int(m.group(1)) if m else 8


def probe(ffprobe_path: str, path: Path) -> ProbeResult:
    data = _run_ffprobe(ffprobe_path, path)
    fmt = data.get("format", {})
    duration = float(fmt.get("duration", 0.0) or 0.0)

    video_stream = None
    audio_streams: list[AudioStream] = []
    subtitle_streams: list[SubtitleStream] = []

    video_pos = 0  # ffmpeg's "v:N" numbering counts every video-type stream, covers included
    video_index = 0
    covers: list[Cover] = []
    attachment_count = 0

    for s in data.get("streams", []):
        codec_type = s.get("codec_type")
        if codec_type == "video":
            if (s.get("disposition") or {}).get("attached_pic"):
                covers.append(Cover(
                    pos=video_pos, codec=s.get("codec_name", ""),
                    filename=(s.get("tags") or {}).get("filename"),
                ))
            elif video_stream is None:
                video_stream = s
                video_index = video_pos
            video_pos += 1
        elif codec_type == "attachment":
            attachment_count += 1
        elif codec_type == "audio":
            tags = s.get("tags") or {}
            audio_streams.append(AudioStream(
                index=len(audio_streams), language=tags.get("language"),
                title=tags.get("title"), default=bool((s.get("disposition") or {}).get("default")),
                codec=s.get("codec_name", ""),
                channels=_to_int(s.get("channels")) or 0,
                channel_layout=s.get("channel_layout"),
                sample_rate=_to_int(s.get("sample_rate")) or 0,
                bits=_to_int(s.get("bits_per_raw_sample")) or _to_int(s.get("bits_per_sample")) or 0,
                # mkv usually has no per-stream bit_rate, but mkvmerge writes a BPS tag
                bitrate=_to_int(s.get("bit_rate")) or _to_int(tags.get("BPS")),
            ))
        elif codec_type == "subtitle":
            tags = s.get("tags") or {}
            subtitle_streams.append(SubtitleStream(
                index=len(subtitle_streams), codec=s.get("codec_name", ""),
                language=tags.get("language"), title=tags.get("title"),
            ))

    if video_stream is None:
        raise MergeError(f"{path.name} non contiene una traccia video.")

    width = int(video_stream.get("width") or 0)
    height = int(video_stream.get("height") or 0)
    if _rotation_degrees(video_stream) in (90, 270):
        width, height = height, width
    # "0/0" is truthy but means "unknown": fall through to r_frame_rate in that case
    rate = next((r for r in (video_stream.get("avg_frame_rate"), video_stream.get("r_frame_rate"))
                 if r and r != "0/0"), "25/1")
    fps = _parse_fps(rate)

    # Video-only bitrate. Many containers (mkv especially) don't declare it per stream;
    # the file's overall bit_rate then includes the audio, so using it as-is would make
    # "keep original bitrate" aim too high by the audio's share - subtract the audio.
    video_bitrate = _to_int(video_stream.get("bit_rate")) or _to_int((video_stream.get("tags") or {}).get("BPS"))
    if video_bitrate is None and _to_int(fmt.get("bit_rate")):
        total = int(fmt["bit_rate"])
        audio_bits = sum(a.bitrate or 64000 * max(a.channels, 1) for a in audio_streams)
        video_bitrate = max(total - audio_bits, total // 2)

    if duration <= 0:
        raise MergeError(f"{path.name}: durata non rilevabile.")
    if width <= 0 or height <= 0:
        raise MergeError(f"{path.name}: risoluzione non rilevabile.")

    chapters = []
    for c in data.get("chapters", []):
        try:
            start, end = float(c["start_time"]), float(c["end_time"])
        except (KeyError, TypeError, ValueError):
            continue
        if end > start:
            chapters.append(Chapter(start=start, end=end, title=(c.get("tags") or {}).get("title")))

    return ProbeResult(
        duration=duration, width=width, height=height, fps=fps,
        video_bitrate=video_bitrate, audio=audio_streams, subtitles=subtitle_streams,
        sar=_parse_sar(video_stream.get("sample_aspect_ratio")), chapters=chapters,
        video_index=video_index, covers=covers, attachment_count=attachment_count,
        interlaced=video_stream.get("field_order") in ("tt", "bb", "tb", "bt"),
        bit_depth=_bit_depth(video_stream),
    )


def _rate_control_args(settings: MergeSettings, bitrate_kbps: int | None) -> list[str]:
    """Build encoder-specific quality/bitrate flags."""
    enc = settings.encoder
    mode = settings.bitrate_mode

    if mode in ("cbr", "original"):
        kbps = bitrate_kbps or settings.cbr_kbps
        if enc == "libsvtav1":
            # SVT-AV1 rejects -maxrate/-bufsize outside CRF mode ("Max Bitrate only supported with CRF mode")
            return ["-b:v", f"{kbps}k"]
        args = [f"-b:v", f"{kbps}k", "-maxrate", f"{kbps}k", "-bufsize", f"{kbps * 2}k"]
        if "_nvenc" in enc or "_amf" in enc:
            args = ["-rc", "cbr"] + args
        return args

    # CRF / constant-quality mode, encoder-specific flag names.
    crf = settings.crf
    if "_nvenc" in enc:
        return ["-rc", "vbr", "-cq", str(crf), "-b:v", "0"]
    if "_qsv" in enc:
        return ["-global_quality", str(crf)]
    if "_amf" in enc:
        return ["-rc", "cqp", "-qp_i", str(crf), "-qp_p", str(crf), "-qp_b", str(crf)]
    # libx264 / libx265 / libsvtav1
    return ["-crf", str(crf)]


def _encoder_preset(settings: MergeSettings) -> str:
    if settings.encoder == "libsvtav1":
        return _SVTAV1_PRESETS.get(settings.preset, "6")
    return settings.preset


def pick_container(source_suffix: str, codec: str, forced: str = "") -> tuple[str, bool]:
    """Output extension for an episode. An explicit choice is honored as-is. For "same as
    the original" the source's extension is used when it can hold the chosen video codec
    (not true for e.g. .webm or .mpg); otherwise .mkv is used. Returns (suffix, changed)."""
    if forced:
        return forced, False
    suffix = source_suffix.lower()
    if suffix in _CONTAINERS_FOR_CODEC.get(codec, set()):
        return source_suffix, False
    return ".mkv", True


def subtitles_for_container(suffix: str, subtitles: list[SubtitleStream]) -> list[SubtitleStream]:
    """The subtitle tracks that can actually be carried into a `suffix` file: everything
    in mkv, text-based ones (converted to mov_text) in mp4/mov, nothing elsewhere (avi,
    flv, wmv reject them, and ts/m2ts would turn them into an unusable data stream)."""
    suffix = suffix.lower()
    if suffix == ".mkv":
        return list(subtitles)
    if suffix in MP4_LIKE_SUFFIXES:
        return [s for s in subtitles if s.codec in _TEXT_SUBTITLE_CODECS]
    return []


@dataclass
class AudioPlan:
    """How one output audio track is produced (the episode's track j drives it)."""
    encoder: str
    layout: str
    sample_rate: int
    bitrate_k: int | None  # None for lossless encoders
    sample_fmt: str | None = None  # forced only for flac, to keep 16 vs 24 bit


_SAFE_LAYOUTS = {"mono", "stereo", "3.0", "4.0", "quad", "5.0", "5.1", "6.1", "7.1"}
_LAYOUT_BY_CHANNELS = {1: "mono", 2: "stereo", 3: "3.0", 4: "4.0", 5: "5.0", 6: "5.1", 7: "6.1", 8: "7.1"}
_AC3_LAYOUTS = {"mono", "stereo", "3.0", "5.0", "5.1"}
_LOSSLESS_SOURCE_CODECS = {"truehd", "mlp", "alac", "wavpack", "flac"}
# source codec -> (ffmpeg encoder, max channels, max kbps)
_SAME_CODEC_ENCODERS = {
    "aac": ("aac", 8, None),
    "ac3": ("ac3", 6, 640),
    "eac3": ("eac3", 8, 1536),
    "mp3": ("libmp3lame", 2, 320),
    "opus": ("libopus", 2, 510),
    "vorbis": ("libvorbis", 2, None),
}


@lru_cache(maxsize=4)
def _available_encoders(ffmpeg_path: str) -> frozenset[str]:
    try:
        out = subprocess.run(
            [ffmpeg_path, "-hide_banner", "-encoders"], capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=15, creationflags=CREATE_NO_WINDOW,
        ).stdout
    except (subprocess.SubprocessError, OSError):
        return frozenset()
    names = set()
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 2 and len(parts[0]) == 6 and parts[0][0] in "VAS":
            names.add(parts[1])
    return frozenset(names)


def _container_allows_audio(suffix: str, codec: str) -> bool:
    if suffix == ".mkv":
        return True
    if suffix in MP4_LIKE_SUFFIXES:
        return codec in ("aac", "ac3", "eac3", "mp3", "opus", "flac")
    return codec in ("aac", "ac3", "mp3")  # avi and anything else: stay conservative


def _layout_for(track: AudioStream | None) -> tuple[int, str]:
    """(channel count, layout name) to use for a track. "(side)" variants are folded
    into the plain layout (the encoders accept those, and swr maps side->back)."""
    if track is None or track.channels <= 0:
        return 2, "stereo"
    layout = (track.channel_layout or "").replace("(side)", "")
    if layout in _SAFE_LAYOUTS:
        return track.channels, layout
    layout = _LAYOUT_BY_CHANNELS.get(track.channels)
    return (track.channels, layout) if layout else (2, "stereo")


def _aac_kbps(channels: int) -> int:
    return 96 * channels if channels <= 2 else 64 * channels


def _coerce_rate(encoder: str, rate: int) -> int:
    if rate <= 0 or encoder == "libopus":
        return TARGET_SAMPLE_RATE
    if encoder in ("ac3", "eac3") and rate not in (32000, 44100, 48000):
        return TARGET_SAMPLE_RATE
    if encoder == "libmp3lame" and rate > 48000:
        return TARGET_SAMPLE_RATE
    return rate


def _plan_audio(
    ffmpeg_path: str, settings: MergeSettings, track: AudioStream | None, suffix: str,
) -> AudioPlan:
    mode = settings.audio_mode
    if mode == "aac_stereo":
        return AudioPlan("aac", "stereo", TARGET_SAMPLE_RATE, STEREO_AAC_KBPS)

    channels, layout = _layout_for(track)
    rate = track.sample_rate if track and track.sample_rate else TARGET_SAMPLE_RATE
    aac_keep = AudioPlan("aac", layout, _coerce_rate("aac", rate), _aac_kbps(channels))
    if mode == "aac_keep" or track is None:
        return aac_keep

    available = _available_encoders(ffmpeg_path)
    lossless_source = track.codec in _LOSSLESS_SOURCE_CODECS or track.codec.startswith("pcm_")
    if mode == "flac" or lossless_source:
        if _container_allows_audio(suffix, "flac") and "flac" in available:
            return AudioPlan(
                "flac", layout, rate, None, sample_fmt="s32" if track.bits > 16 else "s16",
            )
        return aac_keep

    spec = _SAME_CODEC_ENCODERS.get(track.codec)
    if spec is None:
        return aac_keep
    encoder, max_channels, max_kbps = spec
    if (
        channels > max_channels
        or encoder not in available
        or not _container_allows_audio(suffix, track.codec)
        or (encoder in ("ac3", "eac3") and layout not in _AC3_LAYOUTS)
    ):
        return aac_keep
    kbps = round(track.bitrate / 1000) if track.bitrate else _aac_kbps(channels)
    kbps = max(32, min(kbps, max_kbps)) if max_kbps else max(32, kbps)
    return AudioPlan(encoder, layout, _coerce_rate(encoder, rate), kbps)


def _plan_audios(ffmpeg_path: str, settings: MergeSettings, episode: ProbeResult, suffix: str) -> list[AudioPlan]:
    tracks = episode.audio or [None]
    return [_plan_audio(ffmpeg_path, settings, t, suffix) for t in tracks]


def _build_filter_complex(
    segments: list[ProbeResult], episode_idx: int, plans: list[AudioPlan], deinterlace: bool = True,
) -> tuple[str, list[str]]:
    """Returns (filter_complex_string, output_audio_labels). `segments` holds every
    clip being concatenated, in input order - sigla iniziale and sigla finale are
    each optional, so this is 2 or 3 items; `episode_idx` says which one is the real
    episode, whose resolution/fps/audio-track-count drives the output. Every output of
    the graph (video and each audio track) has to be mapped by the caller, otherwise
    ffmpeg fails with an "unconnected" output."""
    episode = segments[episode_idx]
    W, H, FPS = episode.width, episode.height, episode.fps
    n = len(segments)
    parts: list[str] = []

    # Video: scale/pad/fps-normalize each input to the episode's own params, keeping
    # the episode's pixel aspect ratio so its on-screen shape isn't altered.
    sar = episode.sar
    sar_expr = f"{sar.numerator}/{sar.denominator}"
    for i, seg in enumerate(segments):
        # The interlaced flag can't survive re-encoding, so unless it's switched off the
        # fields are merged here (one output frame per input frame, parity auto-detected).
        deint = "yadif=mode=0:parity=-1:deint=0," if deinterlace and seg.interlaced else ""
        if i == episode_idx or seg.sar == sar:
            parts.append(
                f"[{i}:v:{seg.video_index}]{deint}scale=w={W}:h={H}:force_original_aspect_ratio=decrease,"
                f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2:color=black,setsar={sar_expr},fps={FPS}[v{i}]"
            )
        else:
            # Different pixel shape than the episode: fit the clip in square-pixel
            # (on-screen) terms into the episode's on-screen box, then convert back
            # to the episode's stored size and pixel aspect ratio.
            dw = round(W * sar)
            dw += dw % 2
            parts.append(
                f"[{i}:v:{seg.video_index}]{deint}scale=w='iw*sar':h=ih,setsar=1,"
                f"scale=w={dw}:h={H}:force_original_aspect_ratio=decrease,"
                f"pad={dw}:{H}:(ow-iw)/2:(oh-ih)/2:color=black,"
                f"scale=w={W}:h={H},setsar={sar_expr},fps={FPS}[v{i}]"
            )

    # Audio: one output track per episode audio track; sigle reuse/duplicate their
    # own track(s), or generate silence if they have none at all. Every segment is
    # converted to that track's plan (sample rate, channel layout) so concat accepts them.
    n_tracks = max(1, len(episode.audio))
    output_audio_labels = [f"outa{j}" for j in range(n_tracks)]
    for j in range(n_tracks):
        plan = plans[j]
        fmt = f":sample_fmts={plan.sample_fmt}" if plan.sample_fmt else ""
        for seg_i, seg_probe in enumerate(segments):
            if seg_i == episode_idx:
                src = f"[{seg_i}:a:{j}]" if j < len(episode.audio) else None
            else:
                src = f"[{seg_i}:a:{min(j, len(seg_probe.audio) - 1)}]" if seg_probe.audio else None
            label = f"a{seg_i}_{j}"
            if src is None:
                parts.append(
                    f"anullsrc=r={plan.sample_rate}:cl={plan.layout}:"
                    f"d={seg_probe.duration:.3f}"
                    + (f",aformat=sample_fmts={plan.sample_fmt}" if plan.sample_fmt else "")
                    + f"[{label}]"
                )
            else:
                parts.append(
                    f"{src}aresample={plan.sample_rate},"
                    f"aformat=channel_layouts={plan.layout}{fmt}[{label}]"
                )

    # ONE concat for picture and sound together. Separate concats for video and audio
    # let any per-segment length mismatch (audio a bit longer/shorter than the video, or
    # starting late) pile up, so the sigla finale's sound drifted away from its picture;
    # the joint filter lines every segment up on its longest stream instead.
    segment_inputs = "".join(
        f"[v{i}]" + "".join(f"[a{i}_{j}]" for j in range(n_tracks)) for i in range(n)
    )
    outputs = "[outv]" + "".join(f"[{label}]" for label in output_audio_labels)
    parts.append(f"{segment_inputs}concat=n={n}:v=1:a={n_tracks}{outputs}")

    return ";".join(parts), output_audio_labels


def _ffmetadata_escape(text: str) -> str:
    for ch in ("\\", "=", ";", "#", "\n"):
        text = text.replace(ch, "\\" + ch)
    return text


def write_chapters_file(path: Path, chapters: list[Chapter], offset: float) -> None:
    """ffmetadata file holding the episode's chapters shifted by `offset` seconds
    (the sigla iniziale's duration), so they still point at the right scenes."""
    lines = [";FFMETADATA1"]
    for c in chapters:
        lines += [
            "[CHAPTER]", "TIMEBASE=1/1000",
            f"START={round((c.start + offset) * 1000)}", f"END={round((c.end + offset) * 1000)}",
        ]
        if c.title:
            lines.append(f"title={_ffmetadata_escape(c.title)}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def build_command(
    ffmpeg_path: str, segments: list[tuple[Path, ProbeResult]], episode_idx: int,
    output_path: Path, settings: MergeSettings,
    pass_num: int | None = None, passlog_prefix: str | None = None,
    chapters_file: Path | None = None, cover_files: list[CoverFile] = (),
) -> list[str]:
    is_first_pass = pass_num == 1
    probes = [p for _, p in segments]
    episode = probes[episode_idx]
    plans = _plan_audios(ffmpeg_path, settings, episode, output_path.suffix.lower())
    filter_complex, audio_labels = _build_filter_complex(
        probes, episode_idx, plans, deinterlace=settings.deinterlace,
    )

    cmd = [ffmpeg_path, "-y", "-hide_banner", "-loglevel", "error"]
    for path, _ in segments:
        cmd += ["-i", str(path)]
    chapters_input_idx = None
    if chapters_file is not None and not is_first_pass:
        chapters_input_idx = len(segments)
        cmd += ["-f", "ffmetadata", "-i", str(chapters_file)]
    cmd += ["-filter_complex", filter_complex, "-map", "[outv]"]
    # The audio outputs are mapped in the first pass too (and thrown away there): the
    # filtergraph - and with it the video timeline the pass-1 statistics describe - has
    # to be identical in both passes, and an unmapped graph output makes ffmpeg fail.
    for label in audio_labels:
        cmd += ["-map", f"[{label}]"]
    if not is_first_pass:
        # Container-level info (file title etc.) comes from the episode, not from
        # whichever clip happens to be the first input; chapters come from the
        # time-shifted list, or none at all (never from a sigla).
        cmd += ["-map_metadata", str(episode_idx)]
        cmd += ["-map_chapters", str(chapters_input_idx) if chapters_input_idx is not None else "-1"]

    cmd += ["-c:v", settings.encoder, "-preset", _encoder_preset(settings)]
    if settings.tune_animation and settings.encoder in ("libx264", "libx265"):
        cmd += ["-tune", "animation"]
    if settings.keep_10bit and episode.bit_depth >= 10 and settings.encoder in TEN_BIT_ENCODERS:
        cmd += ["-pix_fmt", "yuv420p10le"]

    bitrate_kbps = None
    if settings.bitrate_mode == "original":
        bitrate_kbps = (episode.video_bitrate // 1000) if episode.video_bitrate else settings.cbr_kbps
    cmd += _rate_control_args(settings, bitrate_kbps)

    if pass_num is not None:
        cmd += ["-pass", str(pass_num), "-passlogfile", passlog_prefix]

    if is_first_pass:
        # First pass only needs the video analysis: the audio is cheap raw PCM and the
        # whole output is discarded.
        cmd += ["-c:a", "pcm_s16le", "-progress", "pipe:1", "-nostats", "-f", "null", os.devnull]
    else:
        for j, plan in enumerate(plans):
            cmd += [f"-c:a:{j}", plan.encoder]
            if plan.bitrate_k:
                cmd += [f"-b:a:{j}", f"{plan.bitrate_k}k"]
        # Carry over the episode's attachments (mkv: subtitle fonts) and cover art.
        # Containers that can't hold them just skip. In mkv the attachment streams must
        # come last, and a cover has to be re-attached as a real attachment (copying it
        # as a video stream would turn it into a stray extra video track); mp4 holds
        # cover art as a flagged picture stream, which can be copied directly.
        suffix = output_path.suffix.lower()
        if suffix == ".mkv":
            if episode.attachment_count:
                cmd += ["-map", f"{episode_idx}:t?"]
            for k, cf in enumerate(cover_files):
                t = episode.attachment_count + k
                cmd += [
                    "-attach", str(cf.path),
                    f"-metadata:s:t:{t}", f"mimetype={cf.mimetype}",
                    f"-metadata:s:t:{t}", f"filename={cf.filename}",
                ]
        elif suffix in MP4_LIKE_SUFFIXES:
            for n, cover in enumerate(episode.covers, start=1):
                cmd += [
                    "-map", f"{episode_idx}:v:{cover.pos}",
                    f"-c:v:{n}", "copy", f"-disposition:v:{n}", "attached_pic",
                ]
        # The audio is re-encoded through the filtergraph, so ffmpeg doesn't carry the
        # episode's per-track labels over by itself: set them explicitly.
        set_default_flag = any(a.default for a in episode.audio)
        for j, audio_stream in enumerate(episode.audio or [AudioStream(0, None)]):
            lang = audio_stream.language or "und"
            cmd += [f"-metadata:s:a:{j}", f"language={lang}"]
            if audio_stream.title:
                cmd += [f"-metadata:s:a:{j}", f"title={audio_stream.title}"]
                if output_path.suffix.lower() in MP4_LIKE_SUFFIXES:
                    cmd += [f"-metadata:s:a:{j}", f"handler_name={audio_stream.title}"]
            if set_default_flag:
                cmd += [f"-disposition:a:{j}", "default" if audio_stream.default else "0"]
        cmd += ["-progress", "pipe:1", "-nostats", str(output_path)]

    return cmd


TIME_RE = re.compile(r"out_time_ms=(\d+)")


def run_merge_pass(
    cmd: list[str], expected_duration: float,
    progress_cb: Callable[[float], None] | None,
    cancel_event: threading.Event | None,
) -> None:
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        creationflags=CREATE_NO_WINDOW,
    )
    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            if cancel_event is not None and cancel_event.is_set():
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
                raise Cancelled()
            m = TIME_RE.search(line)
            if m and progress_cb and expected_duration > 0:
                secs = int(m.group(1)) / 1_000_000
                progress_cb(min(1.0, secs / expected_duration))
        proc.wait()
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()

    if proc.returncode != 0:
        stderr = proc.stderr.read() if proc.stderr else ""
        raise MergeError(f"ffmpeg ha fallito (codice {proc.returncode}): {stderr.strip()[-800:]}")


def remux_subtitles(
    ffmpeg_path: str, av_output: Path, episode_path: Path,
    intro_duration: float, subtitles: list[SubtitleStream], final_output: Path,
    attachment_count: int = 0, cover_count: int = 0, cover_files: list[CoverFile] = (),
) -> None:
    """Second lightweight pass: copy the episode's subtitle tracks into the final
    file, shifted by the intro's duration so they stay in sync, no re-encode. The
    first file's attachments and cover art are carried across the same way they
    were put there (see build_command)."""
    suffix = final_output.suffix.lower()
    cmd = [
        ffmpeg_path, "-y", "-hide_banner", "-loglevel", "error",
        "-i", str(av_output),
        "-itsoffset", f"{intro_duration:.3f}", "-i", str(episode_path),
        # Explicit maps instead of a blanket "-map 0": that would also copy mp4's
        # chapter data track (the chapters re-create it, giving a duplicate) and
        # turn an mkv cover into a stray extra video track.
        "-map", "0:v:0", "-map", "0:a", "-map_chapters", "0",
    ]
    if suffix in MP4_LIKE_SUFFIXES:
        for n in range(1, cover_count + 1):
            cmd += ["-map", f"0:v:{n}"]
    for s in subtitles:
        cmd += ["-map", f"1:s:{s.index}"]
    if suffix == ".mkv":  # attachment streams have to come last
        if attachment_count:
            cmd += ["-map", "0:t?"]
        for k, cf in enumerate(cover_files):
            t = attachment_count + k
            cmd += [
                "-attach", str(cf.path),
                f"-metadata:s:t:{t}", f"mimetype={cf.mimetype}",
                f"-metadata:s:t:{t}", f"filename={cf.filename}",
            ]
    cmd += ["-c", "copy"]
    # mp4/mov containers only support the mov_text subtitle codec via copy-compatible path.
    if suffix in MP4_LIKE_SUFFIXES:
        cmd += ["-c:s", "mov_text"]
        for k, s in enumerate(subtitles):
            if s.title:
                cmd += [f"-metadata:s:s:{k}", f"handler_name={s.title}"]
    cmd += [str(final_output)]

    proc = subprocess.run(cmd, capture_output=True, text=True, creationflags=CREATE_NO_WINDOW)
    if proc.returncode != 0:
        raise MergeError(f"Remux sottotitoli fallito: {proc.stderr.strip()[-800:]}")


def verify_output(ffprobe_path: str, output_path: Path, expected_duration: float) -> None:
    if not output_path.exists() or output_path.stat().st_size == 0:
        raise MergeError("File di output mancante o vuoto.")
    result = probe(ffprobe_path, output_path)
    diff = abs(result.duration - expected_duration)
    if diff > max(2.0, expected_duration * DURATION_TOLERANCE):
        raise MergeError(
            f"Durata anomala nell'output ({result.duration:.1f}s attesi ~{expected_duration:.1f}s) "
            "- possibile file troncato o corrotto."
        )


_COVER_FORMATS = {  # codec -> (file extension, mimetype)
    "mjpeg": (".jpg", "image/jpeg"), "png": (".png", "image/png"),
    "bmp": (".bmp", "image/bmp"), "webp": (".webp", "image/webp"),
}


def _extract_cover(
    ffmpeg_path: str, episode_path: Path, cover: Cover, n: int, output_path: Path,
) -> CoverFile | None:
    """Dump an embedded cover image next to the output so it can be attached to an mkv.
    Cover art is a nicety, so any failure here just means no cover, never a failed merge."""
    fmt = _COVER_FORMATS.get(cover.codec)
    if fmt is None:
        return None
    ext, mimetype = fmt
    target = output_path.with_name(f".{output_path.stem}.cover{n}{ext}")
    try:
        proc = subprocess.run(
            [ffmpeg_path, "-y", "-hide_banner", "-loglevel", "error", "-i", str(episode_path),
             "-map", f"0:v:{cover.pos}", "-c", "copy", "-frames:v", "1", "-f", "image2", str(target)],
            capture_output=True, timeout=60, creationflags=CREATE_NO_WINDOW,
        )
    except (subprocess.SubprocessError, OSError):
        target.unlink(missing_ok=True)
        return None
    if proc.returncode != 0 or not target.exists() or target.stat().st_size == 0:
        target.unlink(missing_ok=True)
        return None
    return CoverFile(path=target, mimetype=mimetype, filename=cover.filename or f"cover{ext}")


def merge_episode(
    ffmpeg_path: str, ffprobe_path: str,
    intro_path: Path | None, episode_path: Path, outro_path: Path | None, output_path: Path,
    settings: MergeSettings,
    progress_cb: Callable[[float], None] | None = None,
    cancel_event: threading.Event | None = None,
) -> MergeResult:
    if output_path.exists():
        return MergeResult(output_path=output_path, success=True, skipped=True)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_output = output_path.with_name(f".{output_path.stem}.part{output_path.suffix}")
    passlog_prefix = str(output_path.with_name(f".{output_path.stem}.2pass"))
    chapters_path = output_path.with_name(f".{output_path.stem}.chapters.txt")
    cover_files: list[CoverFile] = []

    use_two_pass = (
        settings.two_pass
        and settings.bitrate_mode in ("cbr", "original")
        and settings.encoder in TWO_PASS_ENCODERS
    )

    try:
        # Sigla iniziale/finale are each optional: build the input list from whichever
        # are actually set, and remember where the episode itself landed in it.
        paths = [p for p in (intro_path, episode_path, outro_path) if p is not None]
        episode_idx = 1 if intro_path is not None else 0
        probes = [probe(ffprobe_path, p) for p in paths]
        segments = list(zip(paths, probes))
        episode = probes[episode_idx]
        expected_duration = sum(p.duration for p in probes)

        chapters_file = None
        if episode.chapters:
            write_chapters_file(
                chapters_path, episode.chapters,
                offset=sum(p.duration for p in probes[:episode_idx]),
            )
            chapters_file = chapters_path

        if output_path.suffix.lower() == ".mkv":
            for n, cover in enumerate(episode.covers):
                cover_file = _extract_cover(ffmpeg_path, episode_path, cover, n, output_path)
                if cover_file is not None:
                    cover_files.append(cover_file)

        if use_two_pass:
            cmd1 = build_command(
                ffmpeg_path, segments, episode_idx, tmp_output, settings,
                pass_num=1, passlog_prefix=passlog_prefix,
            )
            run_merge_pass(
                cmd1, expected_duration,
                (lambda pct: progress_cb(pct * 0.5)) if progress_cb else None,
                cancel_event,
            )
            cmd2 = build_command(
                ffmpeg_path, segments, episode_idx, tmp_output, settings,
                pass_num=2, passlog_prefix=passlog_prefix, chapters_file=chapters_file,
                cover_files=cover_files,
            )
            run_merge_pass(
                cmd2, expected_duration,
                (lambda pct: progress_cb(0.5 + pct * 0.5)) if progress_cb else None,
                cancel_event,
            )
        else:
            cmd = build_command(
                ffmpeg_path, segments, episode_idx, tmp_output, settings,
                chapters_file=chapters_file, cover_files=cover_files,
            )
            run_merge_pass(cmd, expected_duration, progress_cb, cancel_event)

        notes = []
        if settings.deinterlace and any(p.interlaced for p in probes):
            notes.append("video interlacciato: deinterlacciato")

        # Only carry over the subtitle tracks the output container can really hold; an
        # unsupported one would fail the remux, after the whole encode had been done.
        kept_subs = subtitles_for_container(output_path.suffix, episode.subtitles)
        if len(kept_subs) < len(episode.subtitles):
            dropped = len(episode.subtitles) - len(kept_subs)
            notes.append(
                f"{dropped} traccia/e sottotitoli non copiata/e (non supportate dal formato {output_path.suffix})"
            )

        if kept_subs:
            # Subtitle timestamps need shifting only by whatever precedes the episode
            # in the final timeline - that's the sigla iniziale's duration, or 0 if
            # there isn't one.
            intro_duration = probes[0].duration if episode_idx > 0 else 0.0
            # What the intermediate file really contains (a container may have dropped
            # a cover or attachment), rather than what the episode had.
            part = probe(ffprobe_path, tmp_output)
            remux_subtitles(
                ffmpeg_path, tmp_output, episode_path, intro_duration,
                kept_subs, output_path,
                attachment_count=part.attachment_count,
                cover_count=len(part.covers), cover_files=cover_files,
            )
            tmp_output.unlink(missing_ok=True)
        else:
            tmp_output.replace(output_path)

        verify_output(ffprobe_path, output_path, expected_duration)
        return MergeResult(output_path=output_path, success=True, note="; ".join(notes) or None)

    except Cancelled:
        tmp_output.unlink(missing_ok=True)
        output_path.unlink(missing_ok=True)
        return MergeResult(output_path=output_path, success=False, error="Annullato")
    except MergeError as exc:
        tmp_output.unlink(missing_ok=True)
        output_path.unlink(missing_ok=True)
        return MergeResult(output_path=output_path, success=False, error=str(exc))
    finally:
        chapters_path.unlink(missing_ok=True)
        for cf in cover_files:
            cf.path.unlink(missing_ok=True)
        if use_two_pass:
            # A plain prefix match, not Path.glob(): episode filenames routinely
            # contain "[...]" (quality tags, etc.), which glob() would parse as a
            # character class instead of literal text, silently missing the files.
            prefix = Path(passlog_prefix).name
            for entry in os.scandir(output_path.parent):
                if entry.is_file() and entry.name.startswith(prefix):
                    try:
                        os.unlink(entry.path)
                    except OSError:
                        pass
