"""FCP7 XML(xmeml v4) 시퀀스 생성.

프리미어 프로 `파일 > 가져오기`로 이 XML을 열면 무음이 잘려나간 컷,
컷마다 계산된 오디오 레벨, 컷 사이 크로스페이드가 그대로 재현된다.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

from ..loudness import db_to_linear
from ..media import MediaInfo
from ..segments import CutPlan
from ..timecode import FrameRate

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


def _file_element(parent: ET.Element, info: MediaInfo, frame_rate: FrameRate, file_id: str,
                  audio_channels: int, *, full: bool) -> ET.Element:
    if not full:
        return _sub(parent, "file", id=file_id)

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
    start = int(round(cut_frame - half))
    end = int(round(cut_frame + half))
    item = _sub(parent, "transitionitem")
    _sub(item, "start", start)
    _sub(item, "end", end)
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


def _transition_frames(plan: CutPlan, index: int, requested: int) -> int:
    """컷 양쪽에 남은 원본 여유(잘라낸 무음)만큼만 트랜지션을 건다."""
    if requested <= 0 or index + 1 >= len(plan.segments):
        return 0
    rate = plan.frame_rate
    left = plan.segments[index]
    right = plan.segments[index + 1]
    gap_frames = rate.to_frames(right.source_in - left.source_out)
    available = min(
        gap_frames,
        rate.to_frames(left.duration) - 1,
        rate.to_frames(right.duration) - 1,
        rate.to_frames(plan.source_duration - left.source_out) * 2,
    )
    usable = min(requested, max(0, available))
    return usable if usable >= 2 else 0


def render_fcpxml(plan: CutPlan, info: MediaInfo, options: FcpXmlOptions | None = None) -> str:
    options = options or FcpXmlOptions()
    rate = plan.frame_rate
    audio_channels = options.audio_track_count or (info.audio_channels if info.has_audio else 0)
    audio_channels = min(max(audio_channels, 0), 2)

    root = ET.Element("xmeml", {"version": "4"})
    sequence = _sub(root, "sequence", id="sequence-1")
    _sub(sequence, "name", options.sequence_name)
    total_frames = sum(rate.to_frames(seg.duration) for seg in plan.segments)
    _sub(sequence, "duration", total_frames)
    _rate(sequence, rate)
    _timecode(sequence, rate)
    media = _sub(sequence, "media")

    file_written = False

    video = _sub(media, "video")
    format_el = _sub(video, "format")
    sample = _sub(format_el, "samplecharacteristics")
    _rate(sample, rate)
    _sub(sample, "width", info.width or 1920)
    _sub(sample, "height", info.height or 1080)
    _sub(sample, "anamorphic", "FALSE")
    _sub(sample, "pixelaspectratio", "square" if info.pixel_aspect == "square" else info.pixel_aspect)
    _sub(sample, "fielddominance", "none")
    _sub(sample, "colordepth", 24)

    video_track = _sub(video, "track")
    video_ids: list[str] = []
    timeline_frame = 0
    frame_positions: list[tuple[int, int]] = []  # (start, end) in frames

    for index, segment in enumerate(plan.segments):
        length = rate.to_frames(segment.duration)
        frame_positions.append((timeline_frame, timeline_frame + length))
        timeline_frame += length
        video_ids.append(f"clipitem-v{index + 1}")

    if info.has_video:
        for index, segment in enumerate(plan.segments):
            start, end = frame_positions[index]
            clip = _sub(video_track, "clipitem", id=video_ids[index])
            _sub(clip, "name", info.path.name)
            _sub(clip, "enabled", "TRUE")
            _sub(clip, "duration", rate.to_frames(info.duration))
            _rate(clip, rate)
            _sub(clip, "start", start)
            _sub(clip, "end", end)
            _sub(clip, "in", rate.to_frames(segment.source_in))
            _sub(clip, "out", rate.to_frames(segment.source_out))
            _sub(clip, "pproTicksIn", _ticks(segment.source_in))
            _sub(clip, "pproTicksOut", _ticks(segment.source_out))
            _sub(clip, "alphatype", "none")
            _file_element(clip, info, rate, "file-1", audio_channels, full=not file_written)
            file_written = True
            _links(
                clip,
                index + 1,
                video_ids[index],
                [f"clipitem-a{track}-{index + 1}" for track in range(1, audio_channels + 1)],
            )
        if options.video_transition_frames > 0:
            for index in range(len(plan.segments) - 1):
                frames = _transition_frames(plan, index, options.video_transition_frames)
                if frames:
                    _transition(video_track, rate, frame_positions[index][1], frames, "video")
        _sub(video_track, "enabled", "TRUE")
        _sub(video_track, "locked", "FALSE")

    if audio_channels:
        audio = _sub(media, "audio")
        _sub(audio, "numOutputChannels", 2)
        audio_format = _sub(audio, "format")
        audio_sample = _sub(audio_format, "samplecharacteristics")
        _sub(audio_sample, "depth", 16)
        _sub(audio_sample, "samplerate", info.audio_sample_rate or 48000)
        outputs = _sub(audio, "outputs")
        for channel in (1, 2):
            group = _sub(outputs, "group")
            _sub(group, "index", channel)
            _sub(group, "numchannels", 1)
            _sub(group, "downmix", 0)
            channel_el = _sub(group, "channel")
            _sub(channel_el, "index", channel)

        for track_index in range(1, audio_channels + 1):
            track = _sub(audio, "track", currentExplodedTrackIndex=str(track_index - 1),
                         totalExplodedTrackCount=str(audio_channels), premiereTrackType="Stereo")
            for index, segment in enumerate(plan.segments):
                start, end = frame_positions[index]
                clip_id = f"clipitem-a{track_index}-{index + 1}"
                clip = _sub(track, "clipitem", id=clip_id, premiereChannelType="stereo")
                _sub(clip, "name", info.path.name)
                _sub(clip, "enabled", "TRUE")
                _sub(clip, "duration", rate.to_frames(info.duration))
                _rate(clip, rate)
                _sub(clip, "start", start)
                _sub(clip, "end", end)
                _sub(clip, "in", rate.to_frames(segment.source_in))
                _sub(clip, "out", rate.to_frames(segment.source_out))
                _sub(clip, "pproTicksIn", _ticks(segment.source_in))
                _sub(clip, "pproTicksOut", _ticks(segment.source_out))
                _file_element(clip, info, rate, "file-1", audio_channels, full=not file_written)
                file_written = True
                source_track = _sub(clip, "sourcetrack")
                _sub(source_track, "mediatype", "audio")
                _sub(source_track, "trackindex", track_index)
                _links(
                    clip,
                    index + 1,
                    video_ids[index] if info.has_video else None,
                    [f"clipitem-a{t}-{index + 1}" for t in range(1, audio_channels + 1)],
                )
                if options.include_audio_levels:
                    _audio_level_filter(clip, segment.gain_db)
            if options.audio_transition_frames > 0:
                for index in range(len(plan.segments) - 1):
                    frames = _transition_frames(plan, index, options.audio_transition_frames)
                    if frames:
                        _transition(track, rate, frame_positions[index][1], frames, "audio")
            _sub(track, "enabled", "TRUE")
            _sub(track, "locked", "FALSE")
            _sub(track, "outputchannelindex", track_index)

    ET.indent(root, space="\t")
    body = ET.tostring(root, encoding="unicode")
    return '<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE xmeml>\n' + body + "\n"


def write_fcpxml(plan: CutPlan, info: MediaInfo, path: Path,
                 options: FcpXmlOptions | None = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_fcpxml(plan, info, options), encoding="utf-8")
    return path
