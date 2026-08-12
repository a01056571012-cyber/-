"""무음/발화 구간 검출.

엔벨로프(dBFS 배열)만 입력으로 받는 순수 함수라 ffmpeg 없이도 테스트할 수 있다.
"""

from __future__ import annotations

from dataclasses import dataclass

from .audio import Envelope, estimate_noise_floor, estimate_speech_level


@dataclass(frozen=True)
class Threshold:
    open_db: float
    close_db: float
    noise_floor_db: float
    speech_level_db: float
    auto: bool

    def describe(self) -> str:
        mode = "자동" if self.auto else "수동"
        return (
            f"{mode} 임계값 open={self.open_db:.1f}dB close={self.close_db:.1f}dB "
            f"(잡음바닥 {self.noise_floor_db:.1f}dB / 발화 {self.speech_level_db:.1f}dB)"
        )


def choose_threshold(
    envelope: Envelope,
    *,
    threshold_db: float | None = None,
    hysteresis_db: float = 4.0,
    noise_margin_db: float = 9.0,
) -> Threshold:
    """잡음 바닥과 발화 레벨 사이에서 임계값을 정한다.

    수동 값을 주면 그대로 쓰고, 아니면 `잡음바닥 + margin`을 기본으로 하되
    발화 레벨보다 너무 가깝거나 멀지 않도록 보정한다.
    """
    noise = estimate_noise_floor(envelope)
    speech = estimate_speech_level(envelope)

    if threshold_db is not None:
        close = float(threshold_db)
        auto = False
    else:
        auto = True
        close = noise + noise_margin_db
        # 발화와 잡음 차이가 큰 깨끗한 녹음에서는 발화 대비 -26dB 아래로 내려가지 않게 한다.
        close = max(close, speech - 26.0)
        # 반대로 잡음이 큰 녹음에서 발화를 통째로 지우지 않도록 상한을 둔다.
        close = min(close, speech - 8.0)
        close = max(-70.0, min(-18.0, close))

    return Threshold(
        open_db=close + max(0.0, hysteresis_db),
        close_db=close,
        noise_floor_db=noise,
        speech_level_db=speech,
        auto=auto,
    )


def detect_speech_regions(envelope: Envelope, threshold: Threshold) -> list[tuple[float, float]]:
    """히스테리시스 기반 상태머신으로 발화 구간 [start, end)를 뽑는다.

    open_db를 넘으면 발화 시작, close_db 아래로 내려가면 종료로 판정해
    임계값 근처에서 구간이 잘게 쪼개지는 현상을 막는다.
    """
    values = envelope.values_db
    hop = envelope.hop_seconds
    regions: list[tuple[float, float]] = []
    in_speech = False
    start_index = 0

    for index, value in enumerate(values):
        if not in_speech:
            if value >= threshold.open_db:
                in_speech = True
                start_index = index
        elif value < threshold.close_db:
            regions.append((start_index * hop, index * hop))
            in_speech = False

    if in_speech:
        regions.append((start_index * hop, len(values) * hop))

    if envelope.duration:
        regions = [(s, min(e, envelope.duration)) for s, e in regions if s < envelope.duration]
    return [(s, e) for s, e in regions if e > s]


def refine_region_edges(
    envelope: Envelope,
    regions: list[tuple[float, float]],
    threshold: Threshold,
    *,
    lookback_seconds: float = 0.25,
) -> list[tuple[float, float]]:
    """구간 앞뒤로 잡음바닥 근처까지 되짚어 어미/자음이 잘리지 않게 넓힌다.

    open 임계값은 발화 중간 세기를 기준으로 잡히기 때문에, 실제 발화 시작은
    그보다 살짝 앞(약한 자음)에 있다. 잡음바닥 + 3dB 지점까지 확장한다.
    """
    values = envelope.values_db
    hop = envelope.hop_seconds
    if not values or hop <= 0:
        return regions
    # 잡음바닥보다는 확실히 위, 다만 임계값에서 20dB 이내인 소리까지만 되짚는다.
    edge_db = max(threshold.noise_floor_db + 3.0, threshold.close_db - 20.0)
    max_steps = max(1, int(round(lookback_seconds / hop)))
    refined: list[tuple[float, float]] = []

    for start, end in regions:
        i = min(len(values) - 1, max(0, int(round(start / hop))))
        steps = 0
        while i > 0 and steps < max_steps and values[i - 1] >= edge_db:
            i -= 1
            steps += 1
        new_start = i * hop

        j = min(len(values), max(0, int(round(end / hop))))
        steps = 0
        while j < len(values) and steps < max_steps and values[j] >= edge_db:
            j += 1
            steps += 1
        new_end = j * hop
        refined.append((new_start, max(new_start + hop, new_end)))

    return refined
