import pytest

from precut.segments import ShapeOptions, shape_regions
from precut.timecode import FrameRate

RATE = FrameRate(30)


def plan_for(regions, duration=20.0, **kwargs):
    options = ShapeOptions(**{"lead_in": 0.0, "lead_out": 0.0, "min_silence": 0.0,
                              "min_clip": 0.0, **kwargs})
    return shape_regions(regions, duration=duration, options=options, frame_rate=RATE)


def test_padding_extends_both_edges():
    plan = plan_for([(5.0, 6.0)], lead_in=0.2, lead_out=0.4)
    segment = plan.segments[0]
    assert segment.source_in == pytest.approx(4.8, abs=1 / 30)
    assert segment.source_out == pytest.approx(6.4, abs=1 / 30)


def test_padding_is_clamped_at_media_bounds():
    plan = plan_for([(0.05, 19.95)], duration=20.0, lead_in=1.0, lead_out=1.0)
    assert plan.segments[0].source_in == 0.0
    assert plan.segments[0].source_out == pytest.approx(20.0, abs=1 / 30)


def test_short_silence_is_not_cut():
    plan = plan_for([(1.0, 2.0), (2.3, 3.0)], min_silence=0.5)
    assert len(plan.segments) == 1
    assert plan.segments[0].source_out == pytest.approx(3.0, abs=1 / 30)


def test_long_silence_is_cut():
    plan = plan_for([(1.0, 2.0), (5.0, 6.0)], min_silence=0.5)
    assert len(plan.segments) == 2


def test_short_clips_are_dropped():
    plan = plan_for([(1.0, 1.1), (5.0, 7.0)], min_clip=0.5)
    assert len(plan.segments) == 1
    assert plan.segments[0].duration == pytest.approx(2.0, abs=1 / 30)


def test_all_short_clips_keeps_the_longest():
    plan = plan_for([(1.0, 1.1), (5.0, 5.3)], min_clip=5.0)
    assert len(plan.segments) == 1
    assert plan.segments[0].source_in == pytest.approx(5.0, abs=1 / 30)


def test_keep_silence_leaves_breathing_room():
    plan = plan_for([(1.0, 2.0), (6.0, 7.0)], min_silence=0.5, max_silence_keep=1.0)
    assert plan.segments[0].source_out == pytest.approx(2.5, abs=1 / 30)
    assert plan.segments[1].source_in == pytest.approx(5.5, abs=1 / 30)


def test_timeline_is_contiguous_and_frame_aligned():
    plan = plan_for([(1.03, 2.07), (5.11, 6.33), (9.0, 9.7)], min_silence=0.5)
    cursor = 0.0
    for segment in plan.segments:
        assert segment.timeline_start == pytest.approx(cursor)
        cursor += segment.duration
        assert RATE.to_frames(segment.source_in) == pytest.approx(segment.source_in * 30, abs=0.001)
    assert plan.kept_duration == pytest.approx(cursor)
    assert plan.removed_duration == pytest.approx(20.0 - cursor)


def test_segments_never_overlap():
    plan = plan_for([(1.0, 2.0), (2.1, 3.0), (3.05, 4.0)], lead_in=0.5, lead_out=0.5,
                    min_silence=0.05)
    for left, right in zip(plan.segments, plan.segments[1:]):
        assert left.source_out <= right.source_in


def test_removed_ranges_cover_the_gaps():
    plan = plan_for([(2.0, 4.0), (8.0, 10.0)], duration=12.0, min_silence=0.5)
    assert plan.removed == [
        pytest.approx((0.0, 2.0), abs=1 / 30),
        pytest.approx((4.0, 8.0), abs=1 / 30),
        pytest.approx((10.0, 12.0), abs=1 / 30),
    ]


def test_time_map_translates_source_to_timeline():
    plan = plan_for([(2.0, 4.0), (8.0, 10.0)], duration=12.0, min_silence=0.5)
    time_map = plan.time_map()
    assert time_map.map(3.0) == pytest.approx(1.0, abs=1 / 30)
    assert time_map.map(9.0) == pytest.approx(3.0, abs=1 / 30)
    assert time_map.map(6.0) is None
    assert time_map.map_clamped(6.0) == pytest.approx(2.0, abs=1 / 30)
    assert time_map.map_clamped(0.5) == 0.0


def test_time_map_merges_span_pieces_that_meet_at_a_cut():
    # 컷을 걸친 단어는 타임라인에서 이어지므로 한 조각으로 합쳐진다.
    plan = plan_for([(2.0, 4.0), (8.0, 10.0)], duration=12.0, min_silence=0.5)
    pieces = plan.time_map().split_span(3.0, 9.0)
    assert pieces == [pytest.approx((1.0, 3.0), abs=1 / 30)]


def test_time_map_drops_fully_removed_spans():
    plan = plan_for([(2.0, 4.0), (8.0, 10.0)], duration=12.0, min_silence=0.5)
    assert plan.time_map().split_span(5.0, 6.0) == []


def test_negative_option_is_rejected():
    with pytest.raises(ValueError):
        plan_for([(1.0, 2.0)], lead_in=-1.0)
