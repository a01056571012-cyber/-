import math

import pytest

from precut.audio import Envelope, compute_envelope, estimate_noise_floor, percentile
from precut.silence import choose_threshold, detect_speech_regions, refine_region_edges


def make_envelope(pattern: list[tuple[float, float]], hop: float = 0.01) -> Envelope:
    """(dB, 지속시간) 목록으로 엔벨로프를 만든다."""
    values: list[float] = []
    for db, seconds in pattern:
        values.extend([db] * int(round(seconds / hop)))
    return Envelope(values, hop, len(values) * hop)


def test_percentile_interpolates():
    assert percentile([0, 10], 50) == pytest.approx(5.0)
    assert percentile([], 50) == -90.0


def test_choose_threshold_sits_between_noise_and_speech():
    envelope = make_envelope([(-60, 2.0), (-20, 2.0)])
    threshold = choose_threshold(envelope)
    assert threshold.auto
    assert -60 < threshold.close_db < -20
    assert threshold.open_db > threshold.close_db


def test_manual_threshold_is_respected():
    envelope = make_envelope([(-60, 1.0), (-20, 1.0)])
    threshold = choose_threshold(envelope, threshold_db=-35.0, hysteresis_db=5.0)
    assert not threshold.auto
    assert threshold.close_db == -35.0
    assert threshold.open_db == -30.0


def test_detect_regions_finds_speech_blocks():
    envelope = make_envelope(
        [(-60, 1.0), (-20, 2.0), (-60, 1.0), (-20, 1.0), (-60, 0.5)]
    )
    threshold = choose_threshold(envelope, threshold_db=-40.0, hysteresis_db=4.0)
    regions = detect_speech_regions(envelope, threshold)
    assert len(regions) == 2
    assert regions[0] == pytest.approx((1.0, 3.0), abs=0.02)
    assert regions[1] == pytest.approx((4.0, 5.0), abs=0.02)


def test_hysteresis_prevents_chatter_near_threshold():
    # 임계값 바로 근처에서 흔들리는 신호가 여러 조각으로 쪼개지지 않아야 한다.
    pattern = [(-60, 0.5)]
    for _ in range(10):
        pattern += [(-30, 0.05), (-36, 0.05)]
    pattern += [(-60, 0.5)]
    envelope = make_envelope(pattern)
    threshold = choose_threshold(envelope, threshold_db=-38.0, hysteresis_db=6.0)
    regions = detect_speech_regions(envelope, threshold)
    assert len(regions) == 1


def test_regions_are_clamped_to_duration():
    envelope = make_envelope([(-20, 1.0)])
    threshold = choose_threshold(envelope, threshold_db=-40.0)
    regions = detect_speech_regions(envelope, threshold)
    assert regions[0][1] <= envelope.duration + 1e-9


def test_refine_edges_extends_into_quiet_onset():
    envelope = make_envelope([(-70, 0.5), (-45, 0.1), (-20, 0.5), (-70, 0.5)])
    threshold = choose_threshold(envelope, threshold_db=-30.0, hysteresis_db=4.0)
    regions = detect_speech_regions(envelope, threshold)
    refined = refine_region_edges(envelope, regions, threshold)
    assert refined[0][0] < regions[0][0]
    assert refined[0][0] == pytest.approx(0.5, abs=0.03)


def test_compute_envelope_matches_known_levels():
    rate = 8000
    quiet = [0.0] * rate
    loud = [0.5 if i % 2 else -0.5 for i in range(rate)]
    envelope = compute_envelope(quiet + loud, rate, hop_ms=10, window_ms=20)
    assert envelope.duration == pytest.approx(2.0)
    first_half = envelope.values_db[: len(envelope.values_db) // 2 - 2]
    second_half = envelope.values_db[len(envelope.values_db) // 2 + 2 :]
    assert max(first_half) < -80
    assert all(math.isclose(v, -6.02, abs_tol=0.2) for v in second_half)
    assert estimate_noise_floor(envelope) < -80
