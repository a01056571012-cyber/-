"""ffmpeg/ffprobe 실행과 미디어 정보 조회."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from .timecode import FrameRate


class MediaError(RuntimeError):
    """ffmpeg 실행 실패 또는 미디어 해석 실패."""


@dataclass(frozen=True)
class MediaInfo:
    path: Path
    duration: float
    frame_rate: FrameRate
    width: int
    height: int
    has_video: bool
    has_audio: bool
    audio_channels: int
    audio_sample_rate: int
    pixel_aspect: str = "square"

    @property
    def fps(self) -> float:
        return self.frame_rate.fps


def _candidate_paths(name: str, env_var: str) -> list[str]:
    candidates: list[str] = []
    override = os.environ.get(env_var)
    if override:
        candidates.append(override)
    found = shutil.which(name)
    if found:
        candidates.append(found)
    return candidates


def find_ffmpeg() -> str:
    """PATH → PRECUT_FFMPEG → imageio-ffmpeg 번들 순으로 ffmpeg를 찾는다."""
    for candidate in _candidate_paths("ffmpeg", "PRECUT_FFMPEG"):
        if candidate and (os.path.isfile(candidate) or shutil.which(candidate)):
            return candidate
    try:  # 선택적 의존성: 시스템에 ffmpeg가 없을 때의 마지막 수단
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:  # pragma: no cover - 환경 의존
        pass
    raise MediaError(
        "ffmpeg를 찾을 수 없습니다. https://ffmpeg.org 에서 설치하거나 "
        "`pip install imageio-ffmpeg` 후 다시 시도하세요."
    )


def find_ffprobe() -> str | None:
    """ffprobe는 없어도 동작한다(없으면 ffmpeg 로그를 파싱)."""
    for candidate in _candidate_paths("ffprobe", "PRECUT_FFPROBE"):
        if candidate and (os.path.isfile(candidate) or shutil.which(candidate)):
            return candidate
    return None


def run(cmd: Sequence[str], *, capture: bool = True, check: bool = True) -> subprocess.CompletedProcess:
    try:
        proc = subprocess.run(
            list(cmd),
            stdout=subprocess.PIPE if capture else None,
            stderr=subprocess.PIPE if capture else None,
            text=True,
            errors="replace",
        )
    except FileNotFoundError as exc:
        raise MediaError(f"실행 파일을 찾을 수 없습니다: {cmd[0]}") from exc
    if check and proc.returncode != 0:
        tail = (proc.stderr or "").strip().splitlines()[-15:]
        raise MediaError(
            f"명령 실패 (exit {proc.returncode}): {' '.join(cmd[:6])} ...\n" + "\n".join(tail)
        )
    return proc


def probe(path: Path) -> MediaInfo:
    path = Path(path)
    if not path.exists():
        raise MediaError(f"입력 파일이 없습니다: {path}")
    ffprobe = find_ffprobe()
    if ffprobe:
        return _probe_with_ffprobe(ffprobe, path)
    return _probe_with_ffmpeg(find_ffmpeg(), path)


def _parse_rational(value: str | None) -> float | None:
    if not value:
        return None
    if "/" in value:
        num, _, den = value.partition("/")
        try:
            num_f, den_f = float(num), float(den)
        except ValueError:
            return None
        return num_f / den_f if den_f else None
    try:
        return float(value)
    except ValueError:
        return None


def _probe_with_ffprobe(ffprobe: str, path: Path) -> MediaInfo:
    proc = run(
        [ffprobe, "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)]
    )
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise MediaError(f"ffprobe 출력을 해석할 수 없습니다: {path}") from exc

    streams = data.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)

    duration = _parse_rational(data.get("format", {}).get("duration")) or 0.0
    for stream in (video, audio):
        if not duration and stream:
            duration = _parse_rational(stream.get("duration")) or duration

    fps = 30.0
    width = height = 0
    pixel_aspect = "square"
    if video:
        fps = (
            _parse_rational(video.get("avg_frame_rate"))
            or _parse_rational(video.get("r_frame_rate"))
            or 30.0
        )
        if fps <= 0:
            fps = 30.0
        width = int(video.get("width") or 0)
        height = int(video.get("height") or 0)
        sar = video.get("sample_aspect_ratio")
        if sar and sar not in ("1:1", "0:1"):
            pixel_aspect = sar.replace(":", "/")

    return MediaInfo(
        path=path,
        duration=float(duration),
        frame_rate=FrameRate.from_fps(fps),
        width=width,
        height=height,
        has_video=video is not None,
        has_audio=audio is not None,
        audio_channels=int(audio.get("channels") or 0) if audio else 0,
        audio_sample_rate=int(audio.get("sample_rate") or 0) if audio else 0,
        pixel_aspect=pixel_aspect,
    )


_DUR_RE = re.compile(r"Duration:\s*(\d+):(\d+):(\d+\.\d+)")
_VIDEO_RE = re.compile(r"Stream #\d+:\d+.*: Video: .*", re.MULTILINE)
_AUDIO_RE = re.compile(r"Stream #\d+:\d+.*: Audio: .*", re.MULTILINE)
_SIZE_RE = re.compile(r"(\d{2,5})x(\d{2,5})")
_FPS_RE = re.compile(r"(\d+(?:\.\d+)?) (?:fps|tbr)")
_HZ_RE = re.compile(r"(\d+) Hz")


def _probe_with_ffmpeg(ffmpeg: str, path: Path) -> MediaInfo:
    """ffprobe가 없는 환경을 위한 대체 경로 (`ffmpeg -i` 로그 파싱)."""
    proc = run([ffmpeg, "-hide_banner", "-i", str(path)], check=False)
    log = (proc.stderr or "") + (proc.stdout or "")
    if "Invalid data found" in log or "No such file" in log:
        raise MediaError(f"미디어를 열 수 없습니다: {path}")

    duration = 0.0
    match = _DUR_RE.search(log)
    if match:
        duration = int(match.group(1)) * 3600 + int(match.group(2)) * 60 + float(match.group(3))

    video_line = _VIDEO_RE.search(log)
    audio_line = _AUDIO_RE.search(log)

    width = height = 0
    fps = 30.0
    if video_line:
        size = _SIZE_RE.search(video_line.group(0))
        if size:
            width, height = int(size.group(1)), int(size.group(2))
        fps_match = _FPS_RE.search(video_line.group(0))
        if fps_match:
            fps = float(fps_match.group(1)) or 30.0

    channels = 0
    sample_rate = 0
    if audio_line:
        text = audio_line.group(0)
        hz = _HZ_RE.search(text)
        if hz:
            sample_rate = int(hz.group(1))
        if "mono" in text:
            channels = 1
        elif "stereo" in text:
            channels = 2
        elif "5.1" in text:
            channels = 6
        else:
            channels = 2

    return MediaInfo(
        path=path,
        duration=duration,
        frame_rate=FrameRate.from_fps(fps),
        width=width,
        height=height,
        has_video=video_line is not None,
        has_audio=audio_line is not None,
        audio_channels=channels,
        audio_sample_rate=sample_rate,
    )


def extract_analysis_wav(
    path: Path, out_wav: Path, *, sample_rate: int = 16000, ffmpeg: str | None = None
) -> Path:
    """분석용 16kHz 모노 PCM 추출."""
    ffmpeg = ffmpeg or find_ffmpeg()
    out_wav.parent.mkdir(parents=True, exist_ok=True)
    run(
        [
            ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
            "-i", str(path),
            "-vn", "-ac", "1", "-ar", str(sample_rate),
            "-acodec", "pcm_s16le",
            str(out_wav),
        ]
    )
    return out_wav
