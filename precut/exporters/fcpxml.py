"""FCP7 XML(xmeml v4) 시퀀스 생성.

프리미어 프로 `파일 > 가져오기`로 이 XML을 열면 무음이 잘려나간 컷,
컷마다 계산된 오디오 레벨, 컷 사이 크로스페이드가 그대로 재현된다.
원본이 여럿이면 한 시퀀스에 순서대로 이어 붙인다.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

from ..loudness import db_to_linear
from ..media import MediaInfo
from ..timecode import FrameRate
from ..timeline import PlacedClip, Timeline, TimelineItem

PPRO_TICKS_PER_SECOND = 254016000000
MAX_AUDIO_LEVEL = 3.98109  # 프리미어 오디오 레벨 상한 (+12dB)


@dataclass
class FcpXmlOptions:
    sequence_name: str = "precut sequence"
    audio_transition_frames: int = 4
    video_transition_frames: int = 0
    include_audio_levels: bool = True
    audio_track_count: int | None = None


def _sub(parent: ET.Element, tag: str, text: str | int | float | None = None, **attrs) -> ET.Element:
    element = ET.SubElement(parent, tag, {k: str(v) for k, v in attrs.items()})
    if text is not None:
        element.text = str(text)
    return element


def _rate(parent: ET.Element, frame_rate: FrameRate) -> ET.Element:
    rate = _sub(parent, "rate")
    _sub(rate, "timebase", frame_rate.timebase)
    _sub(rate, "ntsc", "TRUE" if frame_rate.ntsc else "FALSE")
    return rate


def _timecode(parent: ET.Element, frame_rate: FrameRate) -> None:
    timecode = _sub(parent, "timecode")
    _rate(timecode, frame_rate)
    _sub(timecode, "string", "00:00:00:00")
    _sub(timecode, "frame", 0)
    _sub(timecode, "displayformat", "DF" if frame_rate.ntsc else "NDF")


def _ticks(seconds: float) -> int:
    return int(round(seconds * PPRO_TICKS_PER_SECOND))


def _file_element(parent: ET.Element, info: MediaInfo, file_id: str, audio_channels: int,
                  *, full: bool) -> ET.Element:
    """파일은 처음 한 번만 전체 정의하고, 이후에는 id로만 참조한다."""
    if not full:
        return _sub(parent, "file", id=file_id)

    frame_rate = info.frame_rate
    file_el = _sub(parent, "file", id=file_id)
    _sub(file_el, "name", info.path.name)
    _sub(file_el, "pathurl", info.path.resolve().as_uri())
    _rate(file_el, frame_rate)
    _sub(file_el, "duration", frame_rate.to_frames(info.duration))
    _timecode(file_el, frame_rate)

    media = _sub(file_el, "media")
    if info.has_video:
        video = _sub(media, "video")
        _sub(video, "duration", frame_rate.to_frames(info.duration))
        sample = _sub(video, "samplecharacteristics")
        _rate(sample, frame_rate)
        _sub(sample, "width", info.width or 1920)
        _sub(sample, "height", info.height or 1080)
        _sub(sample, "anamorphic", "FALSE")
        _sub(sample, "pixelaspectratio", "square" if info.pixel_aspect == "square" else info.pixel_aspect)
        _sub(sample, "fielddominance", "none")
    if info.has_audio:
        audio = _sub(media, "audio")
        sample = _sub(audio, "samplecharacteristics")
        _sub(sample, "depth", 16)
        _sub(sample, "samplerate", info.audio_sample_rate or 48000)
        _sub(audio, "channelcount", audio_channels)
    return file_el


def _audio_level_filter(parent: ET.Element, gain_db: float) -> None:
    level = max(0.0, min(MAX_AUDIO_LEVEL, db_to_linear(gain_db)))
    filter_el = _sub(parent, "filter")
    effect = _sub(filter_el, "effect")
    _sub(effect, "name", "Audio Levels")
    _sub(effect, "effectid", "audiolevels")
    _sub(effect, "effectcategory", "audiolevels")
    _sub(effect, "effecttype", "audiolevels")
    _sub(effect, "mediatype", "audio")
    _sub(effect, "pproBypass", "false")
    parameter = _sub(effect, "parameter", authoringApp="PremierePro")
    _sub(parameter, "parameterid", "level")
    _sub(parameter, "name", "Level")
    _sub(parameter, "valuemin", 0)
    _sub(parameter, "valuemax", MAX_AUDIO_LEVEL)
    _sub(parameter, "value", round(level, 6))


def _links(parent: ET.Element, group_index: int, video_id: str | None, audio_ids: list[str]) -> None:
    """비디오/오디오 클립을 하나의 그룹으로 묶어 함께 선택·이동되게 한다."""
    if video_id:
        link = _sub(parent, "link")
        _sub(link, "linkclipref", video_id)
        _sub(link, "mediatype", "video")
        _sub(link, "trackindex", 1)
        _sub(link, "clipindex", group_index)
    for track_index, audio_id in enumerate(audio_ids, start=1):
        link = _sub(parent, "link")
        _sub(link, "linkclipref", audio_id)
        _sub(link, "mediatype", "audio")
        _sub(link, "trackindex", track_index)
        _sub(link, "clipindex", group_index)
        _sub(link, "groupindex", 1)


def _transition(parent: ET.Element, frame_rate: FrameRate, cut_frame: int, frames: int,
                media_type: str) -> None:
    half = frames / 2.0
    item = _sub(parent, "transitionitem")
    _sub(item, "start", int(round(cut_frame - half)))
    _sub(item, "end", int(round(cut_frame + half)))
    _sub(item, "alignment", "center")
    _sub(item, "cutPointTicks", _ticks(frame_rate.to_seconds(cut_frame)))
    _rate(item, frame_rate)
    effect = _sub(item, "effect")
    if media_type == "video":
        _sub(effect, "name", "Cross Dissolve")
        _sub(effect, "effectid", "Cross Dissolve")
    else:
        _sub(effect, "name", "Cross Fade (+3dB)")
        _sub(effect, "effectid", "KGAudioTransition")
    _sub(effect, "effectcategory", "Dissolve")
    _sub(effect, "effecttype", "transition")
    _sub(effect, "mediatype", media_type)
    _sub(effect, "wipecode", 0)
    _sub(effect, "startratio", 0)
    _sub(effect, "endratio", 1)
    _sub(effect, "reverse", "FALSE")


def _transition_frames(left: PlacedClip, right: PlacedClip, requested: int) -> int:
    """컷 양쪽에 남은 원본 여유(잘라낸 무음)만큼만 트랜지션을 건다.

    서로 다른 원본이 만나는 지점은 여유가 없으므로 하드컷으로 둔다.
    """
    if requested <= 0 or left.item_index != right.item_index:
        return 0
    rate = left.item.info.frame_rate
    gap_frames = rate.to_frames(right.segment.source_in - left.segment.source_out)
    tail_frames = rate.to_frames(left.item.info.duration - left.segment.source_out)
    available = min(
        gap_frames,
        left.length_frames - 1,
        right.length_frames - 1,
        tail_frames * 2,
    )
    usable = min(requested, max(0, available))
    return usable if usable >= 2 else 0


def _add_transitions(track: ET.Element, timeline: Timeline, placed: list[PlacedClip],
                     requested: int, media_type: str) -> None:
    for left, right in zip(placed, placed[1:]):
        frames = _transition_frames(left, right, requested)
        if frames:
            _transition(track, timeline.frame_rate, left.end_frame, frames, media_type)


def _clip_common(clip: ET.Element, placed: PlacedClip, written_files: set[str],
                 audio_channels: int) -> None:
    item: TimelineItem = placed.item
    info = item.info
    source_rate = info.frame_rate
    _sub(clip, "name", info.path.name)
    _sub(clip, "enabled", "TRUE")
    _sub(clip, "duration", source_rate.to_frames(info.duration))
    _rate(clip, source_rate)
    _sub(clip, "start", placed.start_frame)
    _sub(clip, "end", placed.end_frame)
    _sub(clip, "in", source_rate.to_frames(placed.segment.source_in))
    _sub(clip, "out", source_rate.to_frames(placed.segment.source_out))
    _sub(clip, "pproTicksIn", _ticks(placed.segment.source_in))
    _sub(clip, "pproTicksOut", _ticks(placed.segment.source_out))
    _file_element(clip, info, item.file_id, audio_channels, full=item.file_id not in written_files)
    written_files.add(item.file_id)


def render_fcpxml(timeline: Timeline, options: FcpXmlOptions | None = None) -> str:
    options = options or FcpXmlOptions()
    rate = timeline.frame_rate
    placed = timeline.place()
    audio_channels = options.audio_track_count
    if audio_channels is None:
        audio_channels = timeline.audio_channels
    audio_channels = min(max(audio_channels, 0), 2)

    reference = timeline.video_reference
    width = (reference.width if reference else 0) or 1920
    height = (reference.height if reference else 0) or 1080
    pixel_aspect = reference.pixel_aspect if reference else "square"

    root = ET.Element("xmeml", {"version": "4"})
    sequence = _sub(root, "sequence", id="sequence-1")
    _sub(sequence, "name", options.sequence_name or timeline.name)
    _sub(sequence, "duration", timeline.total_frames)
    _rate(sequence, rate)
    _timecode(sequence, rate)
    media = _sub(sequence, "media")

    written_files: set[str] = set()

    video = _sub(media, "video")
    format_el = _sub(video, "format")
    sample = _sub(format_el, "samplecharacteristics")
    _rate(sample, rate)
    _sub(sample, "width", width)
    _sub(sample, "height", height)
    _sub(sample, "anamorphic", "FALSE")
    _sub(sample, "pixelaspectratio", "square" if pixel_aspect == "square" else pixel_aspect)
    _sub(sample, "fielddominance", "none")
    _sub(sample, "colordepth", 24)

    def video_id(clip: PlacedClip) -> str:
        return f"clipitem-v{clip.group_index}"

    def audio_id(clip: PlacedClip, track_index: int) -> str:
        return f"clipitem-a{track_index}-{clip.group_index}"

    if timeline.has_video:
        video_track = _sub(video, "track")
        video_clips = [clip for clip in placed if clip.item.info.has_video]
        for clip in video_clips:
            element = _sub(video_track, "clipitem", id=video_id(clip))
            _clip_common(element, clip, written_files, audio_channels)
            _sub(element, "alphatype", "none")
            _links(
                element,
                clip.group_index,
                video_id(clip),
                [audio_id(clip, t) for t in range(1, audio_channels + 1)],
            )
        _add_transitions(video_track, timeline, video_clips, options.video_transition_frames, "video")
        _sub(video_track, "enabled", "TRUE")
        _sub(video_track, "locked", "FALSE")

    if audio_channels:
        audio = _sub(media, "audio")
        _sub(audio, "numOutputChannels", 2)
        audio_format = _sub(audio, "format")
        audio_sample = _sub(audio_format, "samplecharacteristics")
        _sub(audio_sample, "depth", 16)
        _sub(audio_sample, "samplerate", timeline.sample_rate)
        outputs = _sub(audio, "outputs")
        for channel in (1, 2):
            group = _sub(outputs, "group")
            _sub(group, "index", channel)
            _sub(group, "numchannels", 1)
            _sub(group, "downmix", 0)
            _sub(_sub(group, "channel"), "index", channel)

        audio_clips = [clip for clip in placed if clip.item.info.has_audio]
        for track_index in range(1, audio_channels + 1):
            track = _sub(audio, "track", currentExplodedTrackIndex=str(track_index - 1),
                         totalExplodedTrackCount=str(audio_channels), premiereTrackType="Stereo")
            for clip in audio_clips:
                element = _sub(track, "clipitem", id=audio_id(clip, track_index),
                               premiereChannelType="stereo")
                _clip_common(element, clip, written_files, audio_channels)
                source_track = _sub(element, "sourcetrack")
                _sub(source_track, "mediatype", "audio")
                # 모노 원본은 채널이 하나뿐이라 두 트랙 모두 1번 채널을 읽는다.
                _sub(source_track, "trackindex", min(track_index, max(1, clip.item.info.audio_channels)))
                _links(
                    element,
                    clip.group_index,
                    video_id(clip) if clip.item.info.has_video else None,
                    [audio_id(clip, t) for t in range(1, audio_channels + 1)],
                )
                if options.include_audio_levels:
                    _audio_level_filter(element, clip.segment.gain_db)
            _add_transitions(track, timeline, audio_clips, options.audio_transition_frames, "audio")
            _sub(track, "enabled", "TRUE")
            _sub(track, "locked", "FALSE")
            _sub(track, "outputchannelindex", track_index)

    ET.indent(root, space="\t")
    body = ET.tostring(root, encoding="unicode")
    return '<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE xmeml>\n' + body + "\n"


def write_fcpxml(timeline: Timeline, path: Path, options: FcpXmlOptions | None = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_fcpxml(timeline, options), encoding="utf-8")
    return path
