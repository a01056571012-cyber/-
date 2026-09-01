"""설정 묶음과 영상 유형별 프리셋."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any

from .exporters.fcpxml import FcpXmlOptions
from .loudness import BalanceOptions
from .render import RenderOptions
from .segments import ShapeOptions
from .subtitles import SubtitleOptions
from .transcribe import TranscribeOptions


@dataclass
class DetectOptions:
    threshold_db: float | None = None
    hysteresis_db: float = 4.0
    noise_margin_db: float = 9.0
    hop_ms: float = 10.0
    window_ms: float = 30.0
    analysis_rate: int = 16000


@dataclass
class OutputOptions:
    outdir: Path | None = None
    prefix: str = ""
    write_xml: bool = True
    write_edl: bool = False
    write_jsx: bool = True
    write_srt: bool = True
    write_vtt: bool = False
    write_report: bool = True
    write_script: bool = True
    render_preview: bool = False
    render_audio: bool = False
    burn_subtitles: bool = False
    keep_workdir: bool = False


@dataclass
class Settings:
    detect: DetectOptions = field(default_factory=DetectOptions)
    shape: ShapeOptions = field(default_factory=ShapeOptions)
    balance: BalanceOptions = field(default_factory=BalanceOptions)
    subtitles: SubtitleOptions = field(default_factory=SubtitleOptions)
    transcribe: TranscribeOptions = field(default_factory=TranscribeOptions)
    xml: FcpXmlOptions = field(default_factory=FcpXmlOptions)
    render: RenderOptions = field(default_factory=RenderOptions)
    output: OutputOptions = field(default_factory=OutputOptions)
    make_subtitles: bool = True
    target_duration: float | None = None
    cut_by: str = "silence"  # silence | sentence
    script_path: Path | None = None

    def to_dict(self) -> dict[str, Any]:
        return _serialize(self)

    def apply_overrides(self, overrides: dict[str, Any]) -> "Settings":
        """{'shape': {'lead_in': 0.2}} 형태의 부분 설정을 덮어쓴다.

        밑줄로 시작하는 키는 JSON에 주석을 남기기 위한 것으로 보고 무시한다.
        """
        for group, values in overrides.items():
            if group.startswith("_"):
                continue
            if not hasattr(self, group):
                raise ValueError(f"알 수 없는 설정 그룹: {group}")
            target = getattr(self, group)
            if not is_dataclass(target):
                setattr(self, group, values)
                continue
            valid = {f.name for f in fields(target)}
            for key, value in values.items():
                if key not in valid:
                    raise ValueError(f"알 수 없는 설정: {group}.{key}")
                setattr(target, key, value)
        return self


def _serialize(value: Any) -> Any:
    if is_dataclass(value):
        return {k: _serialize(v) for k, v in asdict(value).items()}
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (list, tuple)):
        return [_serialize(v) for v in value]
    if isinstance(value, dict):
        return {k: _serialize(v) for k, v in value.items()}
    return value


PRESETS: dict[str, dict[str, Any]] = {
    "talking-head": {
        "shape": {"lead_in": 0.12, "lead_out": 0.22, "min_silence": 0.45, "min_clip": 0.30},
        "balance": {"strength": 0.75, "max_boost_db": 9.0},
        "xml": {"audio_transition_frames": 4, "video_transition_frames": 0},
    },
    "vlog": {
        "shape": {"lead_in": 0.18, "lead_out": 0.30, "min_silence": 0.60, "min_clip": 0.40},
        "balance": {"strength": 0.65, "max_boost_db": 8.0},
        "xml": {"audio_transition_frames": 6, "video_transition_frames": 0},
    },
    "lecture": {
        "shape": {"lead_in": 0.20, "lead_out": 0.35, "min_silence": 0.90, "min_clip": 0.50,
                  "max_silence_keep": 0.35},
        "balance": {"strength": 0.85, "max_boost_db": 10.0},
        "subtitles": {"max_chars_per_line": 22, "max_duration": 7.0},
        "xml": {"audio_transition_frames": 6, "video_transition_frames": 0},
    },
    "podcast": {
        "shape": {"lead_in": 0.15, "lead_out": 0.25, "min_silence": 0.70, "min_clip": 0.40},
        "balance": {"strength": 0.9, "max_boost_db": 12.0, "max_cut_db": 12.0},
        "xml": {"audio_transition_frames": 8, "video_transition_frames": 0},
    },
    "tight": {
        "shape": {"lead_in": 0.06, "lead_out": 0.12, "min_silence": 0.25, "min_clip": 0.20},
        "balance": {"strength": 0.8},
        "xml": {"audio_transition_frames": 3, "video_transition_frames": 0},
    },
    "gentle": {
        "shape": {"lead_in": 0.25, "lead_out": 0.40, "min_silence": 1.20, "min_clip": 0.60,
                  "max_silence_keep": 0.5},
        "balance": {"strength": 0.5, "max_boost_db": 6.0},
        "xml": {"audio_transition_frames": 8, "video_transition_frames": 4},
    },
}


def build_settings(preset: str = "talking-head", config_path: Path | None = None) -> Settings:
    if preset not in PRESETS:
        raise ValueError(
            f"알 수 없는 프리셋: {preset} (사용 가능: {', '.join(sorted(PRESETS))})"
        )
    settings = Settings().apply_overrides(PRESETS[preset])
    if config_path:
        data = json.loads(Path(config_path).read_text(encoding="utf-8"))
        settings.apply_overrides(data)
    return settings
