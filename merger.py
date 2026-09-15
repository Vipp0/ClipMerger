"""Core video-merging logic: probing, ffmpeg command construction, execution with
progress reporting, and output integrity verification.

Design: the episode drives the target resolution/fps/audio-track-count. The intro and
outro clips are scaled/resampled to match each episode individually (never the other
way around).
"""
from __future__ import annotations

import json
import re
import subprocess
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from utils import CREATE_NO_WINDOW, CODEC_ENCODERS

TARGET_SAMPLE_RATE = 48000
TARGET_CHANNEL_LAYOUT = "stereo"
AUDIO_BITRATE = "192k"
DURATION_TOLERANCE = 0.02  # 2%


class MergeError(Exception):
    pass


class Cancelled(Exception):
    pass


@dataclass
class AudioStream:
    index: int
    language: str | None


@dataclass
class SubtitleStream:
    index: int
    codec: str
    language: str | None


@dataclass
class ProbeResult:
    duration: float
    width: int
    height: int
    fps: float
    video_bitrate: int | None
    audio: list[AudioStream] = field(default_factory=list)
    subtitles: list[SubtitleStream] = field(default_factory=list)


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


@dataclass
class MergeResult:
    output_path: Path
    success: bool
    skipped: bool = False
    error: str | None = None


def _run_ffprobe(ffprobe_path: str, path: Path) -> dict:
    args = [
        ffprobe_path, "-v", "error", "-print_format", "json",
        "-show_format", "-show_streams", str(path),
    ]
    try:
        proc = subprocess.run(
            args, capture_output=True, text=True, timeout=30,
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


def probe(ffprobe_path: str, path: Path) -> ProbeResult:
    data = _run_ffprobe(ffprobe_path, path)
    fmt = data.get("format", {})
    duration = float(fmt.get("duration", 0.0) or 0.0)

    video_stream = None
    audio_streams: list[AudioStream] = []
    subtitle_streams: list[SubtitleStream] = []

    for s in data.get("streams", []):
        codec_type = s.get("codec_type")
        if codec_type == "video" and video_stream is None:
            video_stream = s
        elif codec_type == "audio":
            lang = (s.get("tags") or {}).get("language")
            audio_streams.append(AudioStream(index=len(audio_streams), language=lang))
        elif codec_type == "subtitle":
            lang = (s.get("tags") or {}).get("language")
            subtitle_streams.append(SubtitleStream(
                index=len(subtitle_streams), codec=s.get("codec_name", ""), language=lang,
            ))

    if video_stream is None:
        raise MergeError(f"{path.name} non contiene una traccia video.")

    width = int(video_stream.get("width") or 0)
    height = int(video_stream.get("height") or 0)
    fps = _parse_fps(video_stream.get("avg_frame_rate") or video_stream.get("r_frame_rate") or "25/1")

    v_bitrate = video_stream.get("bit_rate")
    video_bitrate = int(v_bitrate) if v_bitrate else (int(fmt["bit_rate"]) if fmt.get("bit_rate") else None)

    if duration <= 0:
        raise MergeError(f"{path.name}: durata non rilevabile.")
    if width <= 0 or height <= 0:
        raise MergeError(f"{path.name}: risoluzione non rilevabile.")

    return ProbeResult(
        duration=duration, width=width, height=height, fps=fps,
        video_bitrate=video_bitrate, audio=audio_streams, subtitles=subtitle_streams,
    )


def _rate_control_args(settings: MergeSettings, bitrate_kbps: int | None) -> list[str]:
    """Build encoder-specific quality/bitrate flags."""
    enc = settings.encoder
    mode = settings.bitrate_mode

    if mode in ("cbr", "original"):
        kbps = bitrate_kbps or settings.cbr_kbps
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


def _build_filter_complex(
    intro: ProbeResult, episode: ProbeResult, outro: ProbeResult,
) -> tuple[str, list[str]]:
    """Returns (filter_complex_string, output_audio_labels)."""
    W, H, FPS = episode.width, episode.height, episode.fps
    parts: list[str] = []

    # Video: scale/pad/fps-normalize each of the 3 inputs to the episode's own params.
    for i in range(3):
        parts.append(
            f"[{i}:v:0]scale=w={W}:h={H}:force_original_aspect_ratio=decrease,"
            f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1,fps={FPS}[v{i}]"
        )
    parts.append("[v0][v1][v2]concat=n=3:v=1:a=0[outv]")

    # Audio: one output track per episode audio track; intro/outro reuse/duplicate
    # their own track(s), or generate silence if they have none at all.
    output_audio_labels: list[str] = []
    n_tracks = max(1, len(episode.audio))
    inputs = (intro, None, outro)  # episode itself provides real streams via index 1
    for j in range(n_tracks):
        seg_labels = []
        for seg_i, seg_probe in enumerate((intro, episode, outro)):
            if seg_i == 1:
                src = f"[1:a:{j}]" if j < len(episode.audio) else None
            else:
                src = f"[{seg_i}:a:{min(j, len(seg_probe.audio) - 1)}]" if seg_probe.audio else None
            label = f"a{seg_i}_{j}"
            if src is None:
                parts.append(
                    f"anullsrc=r={TARGET_SAMPLE_RATE}:cl={TARGET_CHANNEL_LAYOUT}:"
                    f"d={seg_probe.duration:.3f}[{label}]"
                )
            else:
                parts.append(
                    f"{src}aresample={TARGET_SAMPLE_RATE},"
                    f"aformat=channel_layouts={TARGET_CHANNEL_LAYOUT}[{label}]"
                )
            seg_labels.append(f"[{label}]")
        out_label = f"outa{j}"
        parts.append("".join(seg_labels) + f"concat=n=3:v=0:a=1[{out_label}]")
        output_audio_labels.append(out_label)

    return ";".join(parts), output_audio_labels


def build_command(
    ffmpeg_path: str, intro_path: Path, episode_path: Path, outro_path: Path,
    output_path: Path, intro: ProbeResult, episode: ProbeResult, outro: ProbeResult,
    settings: MergeSettings,
) -> list[str]:
    filter_complex, audio_labels = _build_filter_complex(intro, episode, outro)

    cmd = [
        ffmpeg_path, "-y", "-hide_banner", "-loglevel", "error",
        "-i", str(intro_path), "-i", str(episode_path), "-i", str(outro_path),
        "-filter_complex", filter_complex,
        "-map", "[outv]",
    ]
    for label in audio_labels:
        cmd += ["-map", f"[{label}]"]

    cmd += ["-c:v", settings.encoder, "-preset", settings.preset]
    if settings.tune_animation and settings.encoder in ("libx264", "libx265"):
        cmd += ["-tune", "animation"]

    bitrate_kbps = None
    if settings.bitrate_mode == "original":
        bitrate_kbps = (episode.video_bitrate // 1000) if episode.video_bitrate else settings.cbr_kbps
    cmd += _rate_control_args(settings, bitrate_kbps)

    cmd += ["-c:a", "aac", "-b:a", AUDIO_BITRATE]
    for j, audio_stream in enumerate(episode.audio or [AudioStream(0, None)]):
        lang = audio_stream.language or "und"
        cmd += [f"-metadata:s:a:{j}", f"language={lang}"]

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
                raise Cancelled()
            m = TIME_RE.search(line)
            if m and progress_cb and expected_duration > 0:
                secs = int(m.group(1)) / 1_000_000
                progress_cb(min(1.0, secs / expected_duration))
        proc.wait()
    finally:
        if proc.poll() is None:
            proc.terminate()

    if proc.returncode != 0:
        stderr = proc.stderr.read() if proc.stderr else ""
        raise MergeError(f"ffmpeg ha fallito (codice {proc.returncode}): {stderr.strip()[-800:]}")


def remux_subtitles(
    ffmpeg_path: str, av_output: Path, episode_path: Path,
    intro_duration: float, subtitles: list[SubtitleStream], final_output: Path,
) -> None:
    """Second lightweight pass: copy the episode's subtitle tracks into the final
    file, shifted by the intro's duration so they stay in sync, no re-encode."""
    cmd = [
        ffmpeg_path, "-y", "-hide_banner", "-loglevel", "error",
        "-i", str(av_output),
        "-itsoffset", f"{intro_duration:.3f}", "-i", str(episode_path),
        "-map", "0",
    ]
    for s in subtitles:
        cmd += ["-map", f"1:s:{s.index}"]
    cmd += ["-c", "copy"]
    # mp4/mov containers only support the mov_text subtitle codec via copy-compatible path.
    if final_output.suffix.lower() in (".mp4", ".mov", ".m4v"):
        cmd += ["-c:s", "mov_text"]
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


def merge_episode(
    ffmpeg_path: str, ffprobe_path: str,
    intro_path: Path, episode_path: Path, outro_path: Path, output_path: Path,
    settings: MergeSettings,
    progress_cb: Callable[[float], None] | None = None,
    cancel_event: threading.Event | None = None,
) -> MergeResult:
    if output_path.exists():
        return MergeResult(output_path=output_path, success=True, skipped=True)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_output = output_path.with_name(f".{output_path.stem}.part{output_path.suffix}")

    try:
        intro = probe(ffprobe_path, intro_path)
        episode = probe(ffprobe_path, episode_path)
        outro = probe(ffprobe_path, outro_path)
        expected_duration = intro.duration + episode.duration + outro.duration

        cmd = build_command(
            ffmpeg_path, intro_path, episode_path, outro_path, tmp_output,
            intro, episode, outro, settings,
        )
        run_merge_pass(cmd, expected_duration, progress_cb, cancel_event)

        if episode.subtitles:
            remux_subtitles(
                ffmpeg_path, tmp_output, episode_path, intro.duration,
                episode.subtitles, output_path,
            )
            tmp_output.unlink(missing_ok=True)
        else:
            tmp_output.replace(output_path)

        verify_output(ffprobe_path, output_path, expected_duration)
        return MergeResult(output_path=output_path, success=True)

    except Cancelled:
        tmp_output.unlink(missing_ok=True)
        output_path.unlink(missing_ok=True)
        return MergeResult(output_path=output_path, success=False, error="Annullato")
    except MergeError as exc:
        tmp_output.unlink(missing_ok=True)
        output_path.unlink(missing_ok=True)
        return MergeResult(output_path=output_path, success=False, error=str(exc))
