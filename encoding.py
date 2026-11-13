"""Le impostazioni di codifica, uguali per tutti gli strumenti che ricodificano (Unisci sigle, Sostituisci sigla,
Dividi puntate): un solo insieme di opzioni (EncodingOptions), i preset già pronti, e la traduzione in
MergeSettings per un file preciso (codec dell'originale, encoder hardware, bitrate dell'originale...)."""
from __future__ import annotations

import dataclasses
from dataclasses import dataclass

import merger
import utils

# Quality levels: the CRF of H.264; other codecs get a nearby value that gives about the same fidelity
# (measured on a cartoon excerpt: H.265 needs one point less, AV1 one more).
QUALITY_LEVELS = [
    ("max", "Massima (file molto grandi)", 14),
    ("high", "Alta", 16),
    ("good", "Buona", 18),
    ("balanced", "Bilanciata", 20),
    ("small", "Compatta", 23),
]
CRF_OFFSET = {"h264": 0, "h265": -1, "av1": 1}
LEVEL_CRF = {key: crf for key, _, crf in QUALITY_LEVELS}
QUALITY_OTHER = [("custom", "Personalizzata (CRF)"), ("cbr", "Bitrate fisso"), ("original", "Come l'originale")]
SPEEDS = [("Veloce", "fast"), ("Bilanciata", "medium"), ("Lenta (qualità migliore)", "slow")]
CODECS = [("Come l'originale", "source"), ("H.264", "h264"), ("H.265 (HEVC)", "h265"), ("AV1", "av1")]
CONTAINERS = [("Come l'originale", ""), (".mp4", ".mp4"), (".mkv", ".mkv"), (".avi", ".avi")]
AUDIO_MODES = [
    ("Come l'originale (stesso codec, canali e bitrate)", "original"),
    ("AAC, mantieni i canali originali (es. 5.1 resta 5.1)", "aac_keep"),
    ("AAC stereo 192k (compatto)", "aac_stereo"),
    ("FLAC senza perdita (file più grandi)", "flac"),
]
DEINTERLACE = [("Automatico (solo se il video è interlacciato)", "auto"), ("Sempre", "always"), ("Mai", "never")]


@dataclass
class EncodingOptions:
    codec: str = "source"        # "source" (the codec of each original) | "h264" | "h265" | "av1"
    speed: str = "medium"        # "fast" | "medium" | "slow"
    quality: str = "balanced"    # a level key, or "custom" (crf), "cbr" (kbps), "original" (the original's bitrate)
    crf: int = 20                # used when quality == "custom"
    kbps: int = 2000             # used when quality == "cbr"
    cap_bitrate: bool = False    # never above the original's video bitrate (constant-quality mode, x264/x265)
    tune_animation: bool = False
    two_pass: bool = False
    ten_bit: bool = True
    deinterlace: str = "auto"    # "auto" | "always" | "never"
    container: str = ""          # "" = like the original
    audio: str = "original"
    use_gpu: bool = False
    parallel: int = 1

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)

    @staticmethod
    def from_dict(d: dict | None) -> "EncodingOptions":
        """Reads saved options, ignoring unknown or invalid entries (a corrupt file never breaks the program)."""
        base = EncodingOptions()
        if not isinstance(d, dict):
            return base
        for f in dataclasses.fields(EncodingOptions):
            if f.name not in d:
                continue
            value, default = d[f.name], getattr(base, f.name)
            try:
                if isinstance(default, bool):
                    value = bool(value)
                elif isinstance(default, int):
                    value = int(value)
                else:
                    value = str(value)
            except (TypeError, ValueError):
                continue
            setattr(base, f.name, value)
        if base.quality not in LEVEL_CRF and base.quality not in dict(QUALITY_OTHER):
            base.quality = "balanced"
        base.parallel = min(max(base.parallel, 1), 8)
        return base

    @property
    def crf_label(self) -> str:
        """The CRF of the chosen level, to show next to it ("" for the other modes)."""
        crf = LEVEL_CRF.get(self.quality)
        if crf is None:
            return ""
        return f"CRF {crf} (H.265: {crf + CRF_OFFSET['h265']}, AV1: {crf + CRF_OFFSET['av1']})"


# ---------------------------------------------------------------------------- ready-made presets
BUILTIN_PRESETS: list[tuple[str, str, EncodingOptions]] = [
    ("standard", "Predefinito", EncodingOptions()),
    ("cartoni", "Cartoni animati", EncodingOptions(quality="high", tune_animation=True)),
    ("piccolo", "File più piccoli (H.265)", EncodingOptions(codec="h265", quality="small", speed="slow", cap_bitrate=True)),
    ("massima", "Massima qualità", EncodingOptions(quality="max", speed="slow")),
]


# ---------------------------------------------------------------------------- from options to what ffmpeg needs
def build_settings(opts: EncodingOptions, info: merger.ProbeResult | None, hw_encoders: dict[str, str | None] | None = None,
                   ffmpeg: str = "") -> tuple[merger.MergeSettings, list[str]]:
    """The MergeSettings for one file. `info` is the original's probe (its codec and bitrate matter for "come
    l'originale"). Returns the settings and remarks worth telling the user (e.g. an unwritable original codec)."""
    notes: list[str] = []
    codec = opts.codec
    if codec == "source":
        codec = merger.app_codec_for(info.video_codec) if info is not None else None
        if codec is None:
            codec = "h265"
            if info is not None:
                notes.append(f"codec originale ({merger.codec_display_name(info.video_codec)}) non riscrivibile: usato H.265")
    hw = (hw_encoders or {}).get(codec) if opts.use_gpu else None
    encoder = hw or utils.CODEC_ENCODERS[codec][0]
    soft = not hw

    mode, crf = "crf", opts.crf
    if opts.quality == "cbr":
        mode = "cbr"
    elif opts.quality == "original":
        mode = "original"
    elif opts.quality != "custom":
        crf = LEVEL_CRF.get(opts.quality, 20) + CRF_OFFSET[codec]
    cap = 0
    if opts.cap_bitrate and mode == "crf" and soft and codec in ("h264", "h265") and info is not None and info.video_bitrate:
        cap = info.video_bitrate // 1000
    settings = merger.MergeSettings(
        codec=codec, encoder=encoder, is_hardware=bool(hw), preset=opts.speed, bitrate_mode=mode, crf=crf,
        cbr_kbps=opts.kbps,
        tune_animation=opts.tune_animation and soft and codec in ("h264", "h265"),
        two_pass=opts.two_pass and soft and mode != "crf",
        audio_mode=opts.audio, deinterlace=opts.deinterlace != "never",
        keep_10bit=opts.ten_bit and soft, max_kbps=cap,
        deint_filter=_deint_filter(ffmpeg) if ffmpeg else "",
    )
    return settings, notes


def _deint_filter(ffmpeg: str) -> str:
    import dividi  # imported here: that module needs this one's siblings, not the other way round
    return dividi.deint_filter(ffmpeg)


def resolve_interlaced(opts: EncodingOptions, ffmpeg: str, path, info: merger.ProbeResult) -> bool:
    """Is this file's picture to be deinterlaced? "always"/"never" as asked; "auto" looks at the file's own flag
    and, when it doesn't declare anything, at a minute of its picture (old TV material is often interlaced
    without saying so)."""
    if opts.deinterlace == "always":
        return True
    if opts.deinterlace == "never":
        return False
    import dividi
    return dividi.detect_interlaced(ffmpeg, path, info.interlaced, info.duration)[0]
