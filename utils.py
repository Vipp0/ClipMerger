"""Helper functions: ffmpeg/ffprobe discovery, hardware encoder detection, misc utilities."""
from __future__ import annotations

import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

VIDEO_EXTENSIONS = {
    ".mp4", ".mkv", ".avi", ".mov", ".ts", ".m2ts", ".wmv", ".flv", ".webm", ".mpg", ".mpeg",
}

CREATE_NO_WINDOW = 0x08000000

# codec -> (software_encoder, {vendor: hw_encoder})
CODEC_ENCODERS = {
    "h264": ("libx264", {"nvidia": "h264_nvenc", "intel": "h264_qsv", "amd": "h264_amf"}),
    "h265": ("libx265", {"nvidia": "hevc_nvenc", "intel": "hevc_qsv", "amd": "hevc_amf"}),
    "av1": ("libsvtav1", {"nvidia": "av1_nvenc", "intel": "av1_qsv", "amd": "av1_amf"}),
}


def find_tool(name: str) -> str | None:
    """Locate an executable (ffmpeg/ffprobe) on PATH."""
    return shutil.which(name)


def resource_path(relative: str) -> str:
    """Resolve a bundled asset path, both when run from source and from a
    PyInstaller-frozen executable (which unpacks data files under _MEIPASS)."""
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return (base / relative).as_posix()


@dataclass
class FfmpegStatus:
    ffmpeg_path: str | None
    ffprobe_path: str | None

    @property
    def ok(self) -> bool:
        return bool(self.ffmpeg_path and self.ffprobe_path)


def check_ffmpeg() -> FfmpegStatus:
    return FfmpegStatus(find_tool("ffmpeg"), find_tool("ffprobe"))


def list_video_files(folder: Path) -> list[Path]:
    return sorted(
        p for p in folder.iterdir()
        if p.is_file() and p.suffix.lower() in VIDEO_EXTENSIONS
    )


def _run_quiet(args: list[str], timeout: float = 8.0) -> subprocess.CompletedProcess:
    return subprocess.run(
        args,
        capture_output=True,
        text=True,
        timeout=timeout,
        creationflags=CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0,
    )


def detect_hw_encoder(ffmpeg_path: str, codec: str) -> str | None:
    """Return the first hardware encoder name for `codec` that actually works on this
    machine, testing each candidate with a tiny real encode. None if none work."""
    _, hw_map = CODEC_ENCODERS[codec]
    try:
        listed = _run_quiet([ffmpeg_path, "-hide_banner", "-encoders"]).stdout
    except (subprocess.SubprocessError, OSError):
        return None

    for encoder_name in hw_map.values():
        if encoder_name not in listed:
            continue
        try:
            test = _run_quiet([
                ffmpeg_path, "-hide_banner", "-loglevel", "error",
                "-f", "lavfi", "-i", "color=c=black:s=64x64:d=0.1",
                "-frames:v", "1", "-c:v", encoder_name, "-f", "null", "-",
            ])
        except (subprocess.SubprocessError, OSError):
            continue
        if test.returncode == 0:
            return encoder_name
    return None


def pick_encoder(ffmpeg_path: str, codec: str, use_gpu: bool) -> tuple[str, bool]:
    """Return (encoder_name, is_hardware). Falls back to software if GPU unavailable."""
    sw_encoder, _ = CODEC_ENCODERS[codec]
    if not use_gpu:
        return sw_encoder, False
    hw_encoder = detect_hw_encoder(ffmpeg_path, codec)
    if hw_encoder:
        return hw_encoder, True
    return sw_encoder, False


def format_duration(seconds: float) -> str:
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h:d}:{m:02d}:{s:02d}"
    return f"{m:d}:{s:02d}"
