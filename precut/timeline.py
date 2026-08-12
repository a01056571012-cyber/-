"""여러 원본을 하나의 시퀀스로 이어붙이는 배치 계산.

컷은 원본마다 따로 계산하지만, 최종 시퀀스에서는 프레임 단위로 빈틈없이
이어져야 한다. 시간(초) 대신 프레임을 기준으로 누적해서 XML과 자막이
같은 위치를 가리키도록 한다.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .media import MediaInfo
from .segments import CutPlan, Segment, TimeMap
from .timecode import FrameRate


@dataclass
class TimelineItem:
    """시퀀스에 올라가는 원본 하나와 그 컷 계획."""

    info: MediaInfo
    plan: CutPlan
    file_id: str = ""


@dataclass
class PlacedClip:
    """시퀀스 위에 프레임 단위로 자리를 잡은 컷 하나."""

    item: TimelineItem
    segment: Segment
    start_frame: int
    end_frame: int
    group_index: int
    item_index: int
    is_first_of_item: bool
    is_last_of_item: bool

    @property
    def length_frames(self) -> int:
        return self.end_frame - self.start_frame


@dataclass
class Timeline:
    items: list[TimelineItem]
    frame_rate: FrameRate
    name: str = "precut sequence"
    _placed: list[PlacedClip] | None = field(default=None, repr=False)

    @property
    def is_merged(self) -> bool:
        return len(self.items) > 1

    @property
    def has_video(self) -> bool:
        return any(item.info.has_video for item in self.items)

    @property
    def audio_channels(self) -> int:
        channels = [item.info.audio_channels for item in self.items if item.info.has_audio]
        return min(2, max(channels)) if channels else 0

    @property
    def sample_rate(self) -> int:
        rates = [item.info.audio_sample_rate for item in self.items if item.info.audio_sample_rate]
        return rates[0] if rates else 48000

    @property
    def video_reference(self) -> MediaInfo | None:
        return next((item.info for item in self.items if item.info.has_video), None)

    def place(self) -> list[PlacedClip]:
        """모든 컷을 프레임 단위로 앞에서부터 붙여 배치한다."""
        if self._placed is not None:
            return self._placed

        placed: list[PlacedClip] = []
        frame = 0
        group = 0
        for item_index, item in enumerate(self.items):
            last = len(item.plan.segments) - 1
            for position, segment in enumerate(item.plan.segments):
                length = max(1, self.frame_rate.to_frames(segment.duration))
                group += 1
                placed.append(
                    PlacedClip(
                        item=item,
                        segment=segment,
                        start_frame=frame,
                        end_frame=frame + length,
                        group_index=group,
                        item_index=item_index,
                        is_first_of_item=position == 0,
                        is_last_of_item=position == last,
                    )
                )
                frame += length
        self._placed = placed
        return placed

    @property
    def total_frames(self) -> int:
        placed = self.place()
        return placed[-1].end_frame if placed else 0

    @property
    def duration(self) -> float:
        return self.frame_rate.to_seconds(self.total_frames)

    @property
    def source_duration(self) -> float:
        return sum(item.info.duration for item in self.items)

    @property
    def removed_duration(self) -> float:
        return max(0.0, self.source_duration - self.duration)

    def time_map(self, item_index: int) -> TimeMap:
        """해당 원본의 시간을 최종 시퀀스 시간으로 옮기는 변환기."""
        segments: list[Segment] = []
        for clip in self.place():
            if clip.item_index != item_index:
                continue
            segments.append(
                Segment(
                    source_in=clip.segment.source_in,
                    source_out=clip.segment.source_out,
                    timeline_start=self.frame_rate.to_seconds(clip.start_frame),
                    gain_db=clip.segment.gain_db,
                )
            )
        return TimeMap(segments)


def build_timeline(
    items: list[tuple[MediaInfo, CutPlan]],
    *,
    name: str = "precut sequence",
    frame_rate: FrameRate | None = None,
) -> Timeline:
    """(미디어 정보, 컷 계획) 목록으로 시퀀스를 만든다.

    시퀀스 프레임레이트는 지정하지 않으면 영상이 있는 첫 원본을 따른다.
    """
    if not items:
        raise ValueError("시퀀스에 넣을 원본이 없습니다.")

    timeline_items: list[TimelineItem] = []
    file_ids: dict[str, str] = {}
    for info, plan in items:
        key = str(info.path)
        if key not in file_ids:
            file_ids[key] = f"file-{len(file_ids) + 1}"
        timeline_items.append(TimelineItem(info=info, plan=plan, file_id=file_ids[key]))

    if frame_rate is None:
        video = next((item.info for item in timeline_items if item.info.has_video), None)
        frame_rate = (video or timeline_items[0].info).frame_rate

    return Timeline(items=timeline_items, frame_rate=frame_rate, name=name)
