import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from precut.exporters.edl import render_edl
from precut.exporters.fcpxml import FcpXmlOptions, render_fcpxml
from precut.exporters.jsx import render_jsx
from precut.media import MediaInfo
from precut.segments import ShapeOptions, shape_regions
from precut.timecode import FrameRate

RATE = FrameRate(30)


def make_info(tmp_path: Path, channels: int = 2, has_video: bool = True) -> MediaInfo:
    path = tmp_path / "촬영본.mp4"
    path.write_bytes(b"stub")
    return MediaInfo(
        path=path,
        duration=20.0,
        frame_rate=RATE,
        width=1920,
        height=1080,
        has_video=has_video,
        has_audio=channels > 0,
        audio_channels=channels,
        audio_sample_rate=48000,
    )


def make_plan(regions=((1.0, 3.0), (6.0, 9.0), (12.0, 14.0)), duration=20.0):
    return shape_regions(
        list(regions),
        duration=duration,
        options=ShapeOptions(lead_in=0.0, lead_out=0.0, min_silence=0.5, min_clip=0.1),
        frame_rate=RATE,
    )


def parse(xml_text: str) -> ET.Element:
    return ET.fromstring(xml_text)


def test_document_header_and_root(tmp_path):
    text = render_fcpxml(make_plan(), make_info(tmp_path))
    assert text.startswith('<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE xmeml>')
    root = parse(text)
    assert root.tag == "xmeml" and root.get("version") == "4"


def test_sequence_duration_matches_kept_frames(tmp_path):
    plan = make_plan()
    root = parse(render_fcpxml(plan, make_info(tmp_path)))
    sequence = root.find("sequence")
    expected = sum(RATE.to_frames(segment.duration) for segment in plan.segments)
    assert int(sequence.findtext("duration")) == expected
    assert int(sequence.find("rate/timebase").text) == 30
    assert sequence.find("rate/ntsc").text == "FALSE"


def test_video_clipitems_carry_source_in_out(tmp_path):
    plan = make_plan()
    root = parse(render_fcpxml(plan, make_info(tmp_path)))
    clips = root.findall("sequence/media/video/track/clipitem")
    assert len(clips) == len(plan.segments)
    for clip, segment in zip(clips, plan.segments):
        assert int(clip.findtext("in")) == RATE.to_frames(segment.source_in)
        assert int(clip.findtext("out")) == RATE.to_frames(segment.source_out)


def test_timeline_positions_are_contiguous(tmp_path):
    plan = make_plan()
    root = parse(render_fcpxml(plan, make_info(tmp_path)))
    clips = root.findall("sequence/media/video/track/clipitem")
    cursor = 0
    for clip in clips:
        assert int(clip.findtext("start")) == cursor
        cursor = int(clip.findtext("end"))
        assert cursor > int(clip.findtext("start"))


def test_file_is_defined_once_and_referenced_afterwards(tmp_path):
    root = parse(render_fcpxml(make_plan(), make_info(tmp_path)))
    files = root.findall(".//file")
    assert len(files) > 1
    full = [f for f in files if f.find("pathurl") is not None]
    assert len(full) == 1
    assert all(f.get("id") == "file-1" for f in files)
    assert full[0].findtext("pathurl").startswith("file://")


def test_pathurl_is_percent_encoded(tmp_path):
    info = make_info(tmp_path)
    root = parse(render_fcpxml(make_plan(), info))
    pathurl = root.find(".//file/pathurl").text
    assert "%" in pathurl  # 한글 파일명이 URL 인코딩되어야 한다
    assert " " not in pathurl


def test_audio_tracks_match_channel_count(tmp_path):
    root = parse(render_fcpxml(make_plan(), make_info(tmp_path, channels=2)))
    tracks = root.findall("sequence/media/audio/track")
    assert len(tracks) == 2
    for index, track in enumerate(tracks, start=1):
        for clip in track.findall("clipitem"):
            assert int(clip.find("sourcetrack/trackindex").text) == index


def test_mono_source_produces_single_audio_track(tmp_path):
    root = parse(render_fcpxml(make_plan(), make_info(tmp_path, channels=1)))
    assert len(root.findall("sequence/media/audio/track")) == 1


def test_audio_only_source_has_no_video_clips(tmp_path):
    info = make_info(tmp_path, channels=2, has_video=False)
    root = parse(render_fcpxml(make_plan(), info))
    assert root.findall("sequence/media/video/track/clipitem") == []
    assert root.findall("sequence/media/audio/track/clipitem")


def test_audio_level_filter_reflects_segment_gain(tmp_path):
    plan = make_plan()
    plan.segments[0].gain_db = 6.0
    plan.segments[1].gain_db = 0.0
    plan.segments[2].gain_db = -6.0
    root = parse(render_fcpxml(plan, make_info(tmp_path, channels=1)))
    clips = root.findall("sequence/media/audio/track/clipitem")
    values = [float(clip.find("filter/effect/parameter/value").text) for clip in clips]
    assert values[0] == pytest.approx(1.995, abs=0.01)
    assert values[1] == pytest.approx(1.0, abs=0.001)
    assert values[2] == pytest.approx(0.501, abs=0.01)
    effect_id = clips[0].findtext("filter/effect/effectid")
    assert effect_id == "audiolevels"


def test_audio_level_is_clamped_to_premiere_range(tmp_path):
    plan = make_plan()
    plan.segments[0].gain_db = 40.0
    root = parse(render_fcpxml(plan, make_info(tmp_path, channels=1)))
    value = float(root.find("sequence/media/audio/track/clipitem/filter/effect/parameter/value").text)
    assert value == pytest.approx(3.98109, abs=0.001)


def test_audio_levels_can_be_disabled(tmp_path):
    options = FcpXmlOptions(include_audio_levels=False)
    root = parse(render_fcpxml(make_plan(), make_info(tmp_path), options))
    assert root.findall(".//filter") == []


def test_video_and_audio_clips_are_linked(tmp_path):
    root = parse(render_fcpxml(make_plan(), make_info(tmp_path, channels=2)))
    first_video = root.find("sequence/media/video/track/clipitem")
    refs = [link.findtext("linkclipref") for link in first_video.findall("link")]
    assert refs == ["clipitem-v1", "clipitem-a1-1", "clipitem-a2-1"]


def test_audio_transitions_are_inserted_at_cuts(tmp_path):
    options = FcpXmlOptions(audio_transition_frames=4, video_transition_frames=0)
    plan = make_plan()
    root = parse(render_fcpxml(plan, make_info(tmp_path, channels=1), options))
    transitions = root.findall("sequence/media/audio/track/transitionitem")
    assert len(transitions) == len(plan.segments) - 1
    first = transitions[0]
    cut_frame = RATE.to_frames(plan.segments[0].duration)
    assert int(first.findtext("start")) == cut_frame - 2
    assert int(first.findtext("end")) == cut_frame + 2
    assert first.findtext("alignment") == "center"
    assert first.findtext("effect/mediatype") == "audio"


def test_video_transitions_are_optional(tmp_path):
    plan = make_plan()
    without = parse(render_fcpxml(plan, make_info(tmp_path), FcpXmlOptions(video_transition_frames=0)))
    assert without.findall("sequence/media/video/track/transitionitem") == []
    with_dissolve = parse(
        render_fcpxml(plan, make_info(tmp_path), FcpXmlOptions(video_transition_frames=6))
    )
    items = with_dissolve.findall("sequence/media/video/track/transitionitem")
    assert len(items) == len(plan.segments) - 1
    assert items[0].findtext("effect/effectid") == "Cross Dissolve"


def test_transition_is_limited_by_available_handles(tmp_path):
    # 컷 사이 무음이 2프레임뿐이면 8프레임짜리 크로스페이드를 걸 수 없다.
    plan = shape_regions(
        [(1.0, 3.0), (3.0 + 2 / 30, 6.0)],
        duration=20.0,
        options=ShapeOptions(lead_in=0.0, lead_out=0.0, min_silence=0.05, min_clip=0.1),
        frame_rate=RATE,
    )
    assert len(plan.segments) == 2
    root = parse(
        render_fcpxml(plan, make_info(tmp_path, channels=1), FcpXmlOptions(audio_transition_frames=8))
    )
    transitions = root.findall("sequence/media/audio/track/transitionitem")
    for item in transitions:
        assert int(item.findtext("end")) - int(item.findtext("start")) <= 2


def test_single_segment_has_no_transitions(tmp_path):
    plan = make_plan(regions=((1.0, 5.0),))
    root = parse(render_fcpxml(plan, make_info(tmp_path), FcpXmlOptions(audio_transition_frames=8)))
    assert root.findall(".//transitionitem") == []


def test_ntsc_rate_is_flagged(tmp_path):
    info = make_info(tmp_path)
    ntsc = FrameRate(30, True)
    plan = shape_regions(
        [(1.0, 3.0)], duration=20.0,
        options=ShapeOptions(lead_in=0, lead_out=0, min_silence=0.5, min_clip=0.1),
        frame_rate=ntsc,
    )
    info = MediaInfo(**{**info.__dict__, "frame_rate": ntsc})
    root = parse(render_fcpxml(plan, info))
    assert root.find("sequence/rate/ntsc").text == "TRUE"
    assert root.find("sequence/timecode/displayformat").text == "DF"


def test_edl_lists_every_segment(tmp_path):
    plan = make_plan()
    text = render_edl(plan, make_info(tmp_path))
    assert "TITLE: precut" in text
    assert text.count("FROM CLIP NAME") == len(plan.segments)
    assert "00:00:01:00 00:00:03:00" in text


def test_jsx_embeds_absolute_paths(tmp_path):
    xml_path = tmp_path / "out.xml"
    srt_path = tmp_path / "out.srt"
    script = render_jsx(xml_path=xml_path, sequence_name="테스트 시퀀스", srt_path=srt_path)
    assert str(xml_path.resolve()) in script
    assert "테스트 시퀀스" in script
    assert "importFiles" in script
    assert "__CONFIG__" not in script
