import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from precut.exporters.edl import render_edl
from precut.exporters.fcpxml import FcpXmlOptions, render_fcpxml
from precut.media import MediaInfo
from precut.segments import ShapeOptions, shape_regions
from precut.timecode import FrameRate
from precut.timeline import build_timeline

RATE = FrameRate(30)


def make_info(tmp_path: Path, name: str, *, duration=20.0, channels=2, fps=RATE,
              has_video=True) -> MediaInfo:
    path = tmp_path / name
    path.write_bytes(b"stub")
    return MediaInfo(
        path=path,
        duration=duration,
        frame_rate=fps,
        width=1920,
        height=1080,
        has_video=has_video,
        has_audio=channels > 0,
        audio_channels=channels,
        audio_sample_rate=48000,
    )


def make_plan(regions, duration=20.0, fps=RATE):
    return shape_regions(
        list(regions),
        duration=duration,
        options=ShapeOptions(lead_in=0.0, lead_out=0.0, min_silence=0.5, min_clip=0.1),
        frame_rate=fps,
    )


def two_clip_timeline(tmp_path):
    first = (make_info(tmp_path, "A.mp4"), make_plan([(1.0, 3.0), (6.0, 8.0)]))
    second = (make_info(tmp_path, "B.mp4"), make_plan([(2.0, 5.0)]))
    return build_timeline([first, second], name="합본")


def test_clips_are_laid_end_to_end(tmp_path):
    timeline = two_clip_timeline(tmp_path)
    placed = timeline.place()
    assert len(placed) == 3
    cursor = 0
    for clip in placed:
        assert clip.start_frame == cursor
        cursor = clip.end_frame
    assert timeline.total_frames == cursor
    # 2초 + 2초 + 3초
    assert timeline.duration == pytest.approx(7.0, abs=0.05)


def test_source_and_removed_durations_cover_every_input(tmp_path):
    timeline = two_clip_timeline(tmp_path)
    assert timeline.source_duration == pytest.approx(40.0)
    assert timeline.removed_duration == pytest.approx(33.0, abs=0.05)


def test_items_are_flagged_by_position(tmp_path):
    placed = two_clip_timeline(tmp_path).place()
    assert [clip.item_index for clip in placed] == [0, 0, 1]
    assert [clip.is_first_of_item for clip in placed] == [True, False, True]
    assert [clip.is_last_of_item for clip in placed] == [False, True, True]


def test_time_map_offsets_later_sources(tmp_path):
    timeline = two_clip_timeline(tmp_path)
    # 두 번째 영상의 원본 3초 지점은 앞 영상 4초가 지난 뒤라 7초여야 한다.
    assert timeline.time_map(1).map(3.0) == pytest.approx(5.0, abs=0.05)
    assert timeline.time_map(0).map(2.0) == pytest.approx(1.0, abs=0.05)
    assert timeline.time_map(1).map(0.5) is None


def test_single_source_timeline_is_not_merged(tmp_path):
    timeline = build_timeline([(make_info(tmp_path, "A.mp4"), make_plan([(1.0, 3.0)]))])
    assert not timeline.is_merged
    assert timeline.frame_rate == RATE


def test_empty_timeline_is_rejected():
    with pytest.raises(ValueError):
        build_timeline([])


def test_sequence_rate_follows_first_video(tmp_path):
    audio_only = (make_info(tmp_path, "A.wav", has_video=False, fps=FrameRate(25)),
                  make_plan([(1.0, 3.0)], fps=FrameRate(25)))
    video = (make_info(tmp_path, "B.mp4", fps=FrameRate(24)), make_plan([(1.0, 3.0)], fps=FrameRate(24)))
    timeline = build_timeline([audio_only, video])
    assert timeline.frame_rate == FrameRate(24)


def test_merged_xml_defines_each_file_once(tmp_path):
    root = ET.fromstring(render_fcpxml(two_clip_timeline(tmp_path)))
    full = [f for f in root.findall(".//file") if f.find("pathurl") is not None]
    assert len(full) == 2
    assert {f.get("id") for f in full} == {"file-1", "file-2"}
    names = [f.findtext("name") for f in full]
    assert names == ["A.mp4", "B.mp4"]


def test_merged_xml_keeps_clip_order_and_positions(tmp_path):
    timeline = two_clip_timeline(tmp_path)
    root = ET.fromstring(render_fcpxml(timeline))
    clips = root.findall("sequence/media/video/track/clipitem")
    assert [clip.findtext("name") for clip in clips] == ["A.mp4", "A.mp4", "B.mp4"]
    cursor = 0
    for clip in clips:
        assert int(clip.findtext("start")) == cursor
        cursor = int(clip.findtext("end"))
    assert int(root.find("sequence").findtext("duration")) == cursor


def test_same_file_twice_reuses_one_file_id(tmp_path):
    info = make_info(tmp_path, "A.mp4")
    timeline = build_timeline([(info, make_plan([(1.0, 3.0)])), (info, make_plan([(6.0, 8.0)]))])
    root = ET.fromstring(render_fcpxml(timeline))
    assert {f.get("id") for f in root.findall(".//file")} == {"file-1"}
    assert len([f for f in root.findall(".//file") if f.find("pathurl") is not None]) == 1


def test_no_transition_between_different_sources(tmp_path):
    timeline = two_clip_timeline(tmp_path)
    root = ET.fromstring(
        render_fcpxml(timeline, FcpXmlOptions(audio_transition_frames=4))
    )
    transitions = root.findall("sequence/media/audio/track[1]/transitionitem")
    # A 영상 안의 컷 경계 한 곳에만 크로스페이드가 걸린다.
    assert len(transitions) == 1
    first_clip_frames = timeline.place()[0].length_frames
    assert int(transitions[0].findtext("start")) == first_clip_frames - 2


def test_merged_clips_use_their_own_source_frame_rate(tmp_path):
    slow = (make_info(tmp_path, "A.mp4", fps=FrameRate(30)), make_plan([(1.0, 3.0)], fps=FrameRate(30)))
    fast = (make_info(tmp_path, "B.mp4", fps=FrameRate(60)), make_plan([(2.0, 4.0)], fps=FrameRate(60)))
    root = ET.fromstring(render_fcpxml(build_timeline([slow, fast])))
    clips = root.findall("sequence/media/video/track/clipitem")
    assert int(clips[0].find("rate/timebase").text) == 30
    assert int(clips[1].find("rate/timebase").text) == 60
    # 60fps 원본의 2초 지점은 120프레임
    assert int(clips[1].findtext("in")) == 120
    # 시퀀스 위 위치는 시퀀스 레이트(30fps) 기준
    assert int(clips[1].findtext("start")) == 60


def test_mono_and_stereo_mix_uses_two_tracks(tmp_path):
    mono = (make_info(tmp_path, "A.mp4", channels=1), make_plan([(1.0, 3.0)]))
    stereo = (make_info(tmp_path, "B.mp4", channels=2), make_plan([(2.0, 4.0)]))
    root = ET.fromstring(render_fcpxml(build_timeline([mono, stereo])))
    tracks = root.findall("sequence/media/audio/track")
    assert len(tracks) == 2
    # 모노 원본은 두 트랙 모두 1번 채널을 읽어야 소리가 사라지지 않는다.
    mono_clips = [c for c in tracks[1].findall("clipitem") if c.findtext("name") == "A.mp4"]
    assert int(mono_clips[0].find("sourcetrack/trackindex").text) == 1


def test_merged_edl_labels_each_reel(tmp_path):
    text = render_edl(two_clip_timeline(tmp_path), title="합본")
    assert "TITLE: 합본" in text
    assert text.count("FROM CLIP NAME: A.mp4") == 2
    assert text.count("FROM CLIP NAME: B.mp4") == 1
