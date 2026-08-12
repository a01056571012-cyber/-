import pytest

from precut.loudness import (
    BalanceOptions,
    LoudnessSample,
    LoudnessTrack,
    apply_segment_balance,
    db_to_linear,
    gated_loudness,
    parse_ebur128_log,
    parse_loudnorm_json,
    weighted_median,
)
from precut.segments import ShapeOptions, shape_regions
from precut.timecode import FrameRate

EBUR128_LOG = """
[Parsed_ebur128_0 @ 0x55] t: 0.4       TARGET:-23 LUFS    M: -22.3 S:-120.7     I: -22.3 LUFS       LRA:   0.0 LU
[Parsed_ebur128_0 @ 0x55] t: 0.5       TARGET:-23 LUFS    M: -21.9 S:-120.7     I: -22.1 LUFS       LRA:   0.0 LU
[Parsed_ebur128_0 @ 0x55] t: 0.6       TARGET:-23 LUFS    M:    -inf S:-120.7   I: -22.1 LUFS       LRA:   0.0 LU
"""


def test_parse_ebur128_log():
    track = parse_ebur128_log(EBUR128_LOG)
    assert len(track.samples) == 3
    assert track.samples[0].time == pytest.approx(0.4)
    assert track.samples[0].momentary == pytest.approx(-22.3)
    assert track.samples[2].momentary == float("-inf")


def test_parse_ebur128_ignores_unrelated_output():
    assert parse_ebur128_log("frame= 100 fps=25 q=-1.0 size=1kB") .samples == []


def test_gated_loudness_ignores_silence():
    assert gated_loudness([]) is None
    assert gated_loudness([float("-inf"), -80.0]) is None
    assert gated_loudness([-20.0, -20.0]) == pytest.approx(-20.0, abs=0.01)


def test_gated_loudness_is_energy_weighted():
    # 큰 소리가 에너지 평균을 지배하고, 상대 게이트가 아주 작은 소리를 배제한다.
    value = gated_loudness([-10.0, -40.0])
    assert value == pytest.approx(-10.0, abs=0.5)


def test_track_segment_loudness_uses_window_alignment():
    samples = [LoudnessSample(time=t / 10, momentary=-30.0 if t < 20 else -12.0, short_term=-20.0)
               for t in range(1, 60)]
    track = LoudnessTrack(samples)
    assert track.segment_loudness(0.0, 1.5) == pytest.approx(-30.0, abs=0.5)
    assert track.segment_loudness(2.5, 5.0) == pytest.approx(-12.0, abs=0.5)
    assert track.segment_loudness(100.0, 101.0) is None


def test_parse_loudnorm_json():
    text = 'random output\n{\n "input_i" : "-27.61",\n "input_tp" : "-9.5",\n' \
           ' "input_lra" : "5.20",\n "input_thresh" : "-37.8",\n "target_offset" : "0.4"\n}\n'
    stats = parse_loudnorm_json(text)
    assert stats.input_i == pytest.approx(-27.61)
    assert stats.target_offset == pytest.approx(0.4)
    chain = stats.to_filter(target_i=-16, target_tp=-1.5, target_lra=11)
    assert "measured_I=-27.61" in chain and "I=-16" in chain


def test_parse_loudnorm_json_missing():
    assert parse_loudnorm_json("no json here") is None


def build_plan():
    return shape_regions(
        [(0.0, 2.0), (4.0, 6.0)],
        duration=8.0,
        options=ShapeOptions(lead_in=0.0, lead_out=0.0, min_silence=0.5, min_clip=0.1),
        frame_rate=FrameRate(30),
    )


def track_with(first_db: float, second_db: float) -> LoudnessTrack:
    samples = []
    for step in range(80):
        time = step / 10.0
        level = first_db if time < 2.0 else second_db
        samples.append(LoudnessSample(time=time + 0.2, momentary=level, short_term=level))
    return LoudnessTrack(samples)


def test_quiet_segment_gets_boosted_and_loud_one_cut():
    plan = build_plan()
    track = track_with(-30.0, -14.0)
    options = BalanceOptions(strength=1.0, reference="program")
    segments, program = apply_segment_balance(plan, track, options)
    assert program is not None
    assert segments[0].gain_db > 0
    assert segments[1].gain_db < 0
    assert segments[0].loudness_lufs == pytest.approx(-30.0, abs=0.5)


def test_gain_is_limited_by_max_boost():
    plan = build_plan()
    track = track_with(-55.0, -10.0)
    segments, _ = apply_segment_balance(
        plan, track, BalanceOptions(strength=1.0, max_boost_db=6.0, max_cut_db=3.0)
    )
    assert segments[0].gain_db == 6.0
    assert segments[1].gain_db == -3.0


def test_strength_scales_the_correction():
    plan = build_plan()
    track = track_with(-30.0, -14.0)
    full, _ = apply_segment_balance(plan, track, BalanceOptions(strength=1.0, max_boost_db=30))
    full_gain = full[0].gain_db
    plan = build_plan()
    half, _ = apply_segment_balance(plan, track, BalanceOptions(strength=0.5, max_boost_db=30))
    assert half[0].gain_db == pytest.approx(full_gain / 2, abs=0.05)


def test_target_reference_uses_absolute_value():
    plan = build_plan()
    track = track_with(-30.0, -30.0)
    segments, _ = apply_segment_balance(
        plan, track, BalanceOptions(strength=1.0, reference="target", target_lufs=-16.0,
                                    max_boost_db=30)
    )
    assert segments[0].gain_db == pytest.approx(14.0, abs=0.5)


def test_balance_disabled_leaves_gain_untouched():
    plan = build_plan()
    segments, _ = apply_segment_balance(plan, track_with(-40.0, -10.0),
                                        BalanceOptions(enabled=False))
    assert all(segment.gain_db == 0.0 for segment in segments)


def test_missing_measurement_falls_back_to_unity_gain():
    plan = build_plan()
    segments, program = apply_segment_balance(plan, LoudnessTrack([]), BalanceOptions())
    assert program is None
    assert all(segment.gain_db == 0.0 for segment in segments)


def test_weighted_median_prefers_longer_segments():
    assert weighted_median([(-30.0, 1.0), (-14.0, 1.0)]) == pytest.approx(-22.0)
    assert weighted_median([(-30.0, 10.0), (-14.0, 1.0)]) < -27.0
    assert weighted_median([(-20.0, 3.0)]) == pytest.approx(-20.0)
    assert weighted_median([]) is None
    assert weighted_median([(float("-inf"), 5.0)]) is None


def test_db_to_linear():
    assert db_to_linear(0) == pytest.approx(1.0)
    assert db_to_linear(6.0) == pytest.approx(1.995, abs=0.01)
