"""검출된 발화 구간을 '자연스러운 컷'으로 다듬고 타임라인에 배치한다."""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass, field

from .timecode import FrameRate


@dataclass
class Segment:
    """원본에서 살아남은 한 컷."""

    source_in: float
    source_out: float
    timeline_start: float = 0.0
    gain_db: float = 0.0
    loudness_lufs: float | None = None

    @property
    def duration(self) -> float:
        return max(0.0, self.source_out - self.source_in)

    @property
    def timeline_end(self) -> float:
        return self.timeline_start + self.duration


@dataclass
class ShapeOptions:
    """컷을 다듬는 규칙 (모두 초 단위)."""

    lead_in: float = 0.12
    lead_out: float = 0.22
    min_silence: float = 0.45
    min_clip: float = 0.30
    max_silence_keep: float = 0.0
    head_pad: float = 0.0

    def validate(self) -> None:
        for name in ("lead_in", "lead_out", "min_silence", "min_clip", "max_silence_keep"):
            if getattr(self, name) < 0:
                raise ValueError(f"{name}은(는) 0 이상이어야 합니다")


@dataclass
class CutPlan:
    segments: list[Segment]
    source_duration: float
    frame_rate: FrameRate
    removed: list[tuple[float, float]] = field(default_factory=list)

    @property
    def kept_duration(self) -> float:
        return sum(seg.duration for seg in self.segments)

    @property
    def removed_duration(self) -> float:
        return max(0.0, self.source_duration - self.kept_duration)

    @property
    def cut_count(self) -> int:
        return len(self.segments)

    def time_map(self) -> "TimeMap":
        return TimeMap(self.segments)


def shape_regions(
    regions: list[tuple[float, float]],
    *,
    duration: float,
    options: ShapeOptions,
    frame_rate: FrameRate,
) -> CutPlan:
    """패딩 → 병합 → 최소길이 필터 → 프레임 스냅 순으로 컷을 정리한다."""
    options.validate()

    padded: list[tuple[float, float]] = []
    for start, end in sorted(regions):
        s = max(0.0, start - options.lead_in)
        e = min(duration, end + options.lead_out)
        if e > s:
            padded.append((s, e))

    merged: list[list[float]] = []
    for start, end in padded:
        if merged and start - merged[-1][1] < options.min_silence:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])

    # 잘라내되 완전히 붙이지는 않는 모드: 무음을 max_silence_keep 만큼 남긴다.
    if options.max_silence_keep > 0 and merged:
        spaced: list[list[float]] = [merged[0]]
        for current in merged[1:]:
            gap = current[0] - spaced[-1][1]
            if gap > options.max_silence_keep:
                keep = options.max_silence_keep / 2.0
                spaced[-1][1] = min(duration, spaced[-1][1] + keep)
                current[0] = max(0.0, current[0] - keep)
            spaced.append(current)
        merged = spaced

    kept = [pair for pair in merged if (pair[1] - pair[0]) >= options.min_clip]
    if not kept and merged:
        kept = [max(merged, key=lambda pair: pair[1] - pair[0])]

    if kept and options.head_pad > 0:
        kept[0][0] = max(0.0, kept[0][0] - options.head_pad)

    segments: list[Segment] = []
    timeline = 0.0
    previous_out = 0.0
    for start, end in kept:
        snapped_in = max(0.0, frame_rate.floor_frames(start) / frame_rate.fps)
        snapped_out = min(duration, frame_rate.ceil_frames(end) / frame_rate.fps)
        snapped_in = max(snapped_in, previous_out)
        if frame_rate.to_frames(snapped_out - snapped_in) < 1:
            continue
        segment = Segment(source_in=snapped_in, source_out=snapped_out, timeline_start=timeline)
        segments.append(segment)
        timeline += segment.duration
        previous_out = snapped_out

    removed: list[tuple[float, float]] = []
    cursor = 0.0
    for segment in segments:
        if segment.source_in > cursor:
            removed.append((cursor, segment.source_in))
        cursor = segment.source_out
    if duration > cursor:
        removed.append((cursor, duration))

    return CutPlan(
        segments=segments, source_duration=duration, frame_rate=frame_rate, removed=removed
    )


class TimeMap:
    """원본 시간 → 컷 편집 후 타임라인 시간 변환기."""

    def __init__(self, segments: list[Segment]):
        self._segments = segments
        self._starts = [seg.source_in for seg in segments]

    def map(self, source_time: float) -> float | None:
        """잘려나간 구간이면 None."""
        index = bisect_right(self._starts, source_time) - 1
        if index < 0:
            return None
        segment = self._segments[index]
        if source_time > segment.source_out:
            return None
        return segment.timeline_start + (source_time - segment.source_in)

    def map_clamped(self, source_time: float) -> float:
        """잘린 구간이면 가장 가까운 살아있는 경계로 붙인다."""
        if not self._segments:
            return 0.0
        if source_time <= self._segments[0].source_in:
            return 0.0
        index = bisect_right(self._starts, source_time) - 1
        segment = self._segments[index]
        if source_time <= segment.source_out:
            return segment.timeline_start + (source_time - segment.source_in)
        if index + 1 < len(self._segments):
            return self._segments[index + 1].timeline_start
        return segment.timeline_end

    def split_span(self, start: float, end: float) -> list[tuple[float, float]]:
        """원본 구간 [start,end)를 타임라인상의 살아있는 조각들로 나눈다."""
        pieces: list[tuple[float, float]] = []
        for segment in self._segments:
            lo = max(start, segment.source_in)
            hi = min(end, segment.source_out)
            if hi > lo:
                pieces.append(
                    (
                        segment.timeline_start + (lo - segment.source_in),
                        segment.timeline_start + (hi - segment.source_in),
                    )
                )
        return _merge_adjacent(pieces)


def _merge_adjacent(pieces: list[tuple[float, float]], tolerance: float = 1e-6) -> list[tuple[float, float]]:
    merged: list[list[float]] = []
    for start, end in pieces:
        if merged and start - merged[-1][1] <= tolerance:
            merged[-1][1] = end
        else:
            merged.append([start, end])
    return [(s, e) for s, e in merged]
