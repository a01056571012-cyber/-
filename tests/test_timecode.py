import pytest

from precut.timecode import FrameRate, format_duration, srt_timestamp


def test_from_fps_detects_ntsc():
    rate = FrameRate.from_fps(29.97002997)
    assert (rate.timebase, rate.ntsc) == (30, True)
    assert rate.fps == pytest.approx(29.97, abs=0.001)


def test_from_fps_integer():
    assert FrameRate.from_fps(25.0) == FrameRate(25, False)
    assert FrameRate.from_fps(23.976) == FrameRate(24, True)


def test_frame_rounding_modes():
    rate = FrameRate(30)
    assert rate.to_frames(1.0) == 30
    assert rate.floor_frames(1.04) == 31
    assert rate.ceil_frames(1.01) == 31
    assert rate.to_seconds(45) == pytest.approx(1.5)


def test_snap_to_frame_grid():
    rate = FrameRate(24)
    assert rate.snap(1.02) == pytest.approx(1.0 + 0 / 24, abs=1e-6)
    assert rate.snap(1.03) == pytest.approx(25 / 24, abs=1e-6)


def test_timecode_non_drop():
    assert FrameRate(25).timecode(61.4) == "00:01:01:10"


def test_timecode_drop_frame_marker():
    tc = FrameRate(30, True).timecode(60.0)
    assert ";" in tc


def test_srt_timestamp():
    assert srt_timestamp(3661.5) == "01:01:01,500"
    assert srt_timestamp(-1) == "00:00:00,000"


def test_format_duration():
    assert format_duration(75.2) == "1:15.2"
    assert format_duration(3675.0) == "1:01:15.0"
