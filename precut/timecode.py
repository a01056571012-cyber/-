"""프레임/타임코드 변환 유틸리티.

프리미어(FCP7 XML)는 모든 시간을 '프레임 수'로 표현하므로, 초 단위 분석 결과를
프레임 격자에 정확히 맞추는 일이 파이프라인 전체의 기준이 된다.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

NTSC_TIMEBASES = {24: 23.976023976023978, 30: 29.97002997002997, 60: 59.94005994005994}


@dataclass(frozen=True)
class FrameRate:
    """정수 timebase + NTSC 드롭 여부로 표현한 프레임레이트."""

    timebase: int
    ntsc: bool = False

    @property
    def fps(self) -> float:
        if self.ntsc:
            return self.timebase * 1000.0 / 1001.0
        return float(self.timebase)

    @classmethod
    def from_fps(cls, fps: float) -> "FrameRate":
        """실측 fps(예: 29.97002997)를 timebase/ntsc 조합으로 되돌린다."""
        if fps <= 0:
            raise ValueError(f"fps는 0보다 커야 합니다: {fps}")
        for timebase, ntsc_fps in NTSC_TIMEBASES.items():
            if abs(fps - ntsc_fps) < 0.01:
                return cls(timebase, True)
        timebase = int(round(fps))
        if abs(fps - timebase) < 0.001:
            return cls(timebase, False)
        # 25.5fps 같은 비표준 값은 올림해서 정수 timebase로 근사한다.
        return cls(max(1, int(math.ceil(fps))), False)

    def to_frames(self, seconds: float) -> int:
        return int(round(seconds * self.fps))

    def floor_frames(self, seconds: float) -> int:
        return int(math.floor(seconds * self.fps + 1e-9))

    def ceil_frames(self, seconds: float) -> int:
        return int(math.ceil(seconds * self.fps - 1e-9))

    def to_seconds(self, frames: int) -> float:
        return frames / self.fps

    def snap(self, seconds: float) -> float:
        """가장 가까운 프레임 경계로 스냅한 초 값."""
        return self.to_seconds(self.to_frames(seconds))

    def timecode(self, seconds: float) -> str:
        """HH:MM:SS:FF 타임코드. NTSC는 드롭프레임(;) 표기를 쓴다."""
        total = self.to_frames(seconds)
        tb = self.timebase
        if self.ntsc and tb in (30, 60):
            return _drop_frame_timecode(total, tb)
        frames = total % tb
        total_seconds = total // tb
        return "%02d:%02d:%02d:%02d" % (
            total_seconds // 3600,
            (total_seconds // 60) % 60,
            total_seconds % 60,
            frames,
        )


def _drop_frame_timecode(total_frames: int, timebase: int) -> str:
    drop = 2 * (timebase // 30)
    frames_per_10min = timebase * 600 - 9 * drop
    frames_per_min = timebase * 60 - drop
    ten_min_blocks, rest = divmod(total_frames, frames_per_10min)
    if rest >= drop:
        extra_min = (rest - drop) // frames_per_min
    else:
        extra_min = 0
    dropped = drop * (9 * ten_min_blocks + extra_min)
    adjusted = total_frames + dropped
    frames = adjusted % timebase
    total_seconds = adjusted // timebase
    return "%02d:%02d:%02d;%02d" % (
        total_seconds // 3600,
        (total_seconds // 60) % 60,
        total_seconds % 60,
        frames,
    )


def srt_timestamp(seconds: float) -> str:
    """SRT용 HH:MM:SS,mmm."""
    seconds = max(0.0, seconds)
    millis = int(round(seconds * 1000))
    hours, millis = divmod(millis, 3600_000)
    minutes, millis = divmod(millis, 60_000)
    secs, millis = divmod(millis, 1000)
    return "%02d:%02d:%02d,%03d" % (hours, minutes, secs, millis)


def vtt_timestamp(seconds: float) -> str:
    return srt_timestamp(seconds).replace(",", ".")


def format_duration(seconds: float) -> str:
    """사람이 읽기 좋은 '1시간 02분 03.4초' 대신 간결한 1:02:03.4 형태."""
    sign = "-" if seconds < 0 else ""
    seconds = abs(seconds)
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    rest = seconds % 60
    if hours:
        return f"{sign}{hours}:{minutes:02d}:{rest:04.1f}"
    return f"{sign}{minutes}:{rest:04.1f}"
