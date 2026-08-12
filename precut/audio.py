"""WAV 로딩과 dBFS 엔벨로프 계산.

numpy가 있으면 벡터 연산으로, 없으면 표준 라이브러리만으로 동작한다.
"""

from __future__ import annotations

import array
import math
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

try:  # 선택적 가속
    import numpy as _np
except Exception:  # pragma: no cover - numpy 없는 환경
    _np = None

SILENCE_FLOOR_DB = -90.0


@dataclass
class Envelope:
    """일정 간격(hop)마다 계산한 프레임별 dBFS 값."""

    values_db: list[float]
    hop_seconds: float
    duration: float

    def time_at(self, index: int) -> float:
        return index * self.hop_seconds

    def __len__(self) -> int:  # pragma: no cover - 편의 메서드
        return len(self.values_db)


def read_wav_mono(path: Path) -> tuple[Sequence[float], int]:
    """16bit PCM WAV를 -1..1 범위 모노 샘플로 읽는다."""
    with wave.open(str(path), "rb") as wav:
        channels = wav.getnchannels()
        width = wav.getsampwidth()
        rate = wav.getframerate()
        frames = wav.readframes(wav.getnframes())
    if width != 2:
        raise ValueError(f"16bit PCM WAV만 지원합니다 (sampwidth={width})")

    if _np is not None:
        data = _np.frombuffer(frames, dtype="<i2").astype("float32") / 32768.0
        if channels > 1:
            usable = (len(data) // channels) * channels
            data = data[:usable].reshape(-1, channels).mean(axis=1)
        return data, rate

    samples = array.array("h")
    samples.frombytes(frames)
    values = [s / 32768.0 for s in samples]
    if channels > 1:
        mixed = []
        for i in range(0, len(values) - channels + 1, channels):
            mixed.append(sum(values[i : i + channels]) / channels)
        values = mixed
    return values, rate


def compute_envelope(samples: Sequence[float], sample_rate: int, *, hop_ms: float = 10.0, window_ms: float = 30.0) -> Envelope:
    """윈도우 RMS를 dBFS로 변환한 엔벨로프."""
    hop = max(1, int(round(sample_rate * hop_ms / 1000.0)))
    window = max(hop, int(round(sample_rate * window_ms / 1000.0)))
    total = len(samples)
    duration = total / sample_rate if sample_rate else 0.0

    if _np is not None and not isinstance(samples, list):
        data = _np.asarray(samples, dtype="float32")
        if total == 0:
            return Envelope([], hop / sample_rate, 0.0)
        power = _np.square(data, dtype="float32")
        cumulative = _np.concatenate(([_np.float32(0)], _np.cumsum(power, dtype="float64")))
        starts = _np.arange(0, total, hop)
        half = window // 2
        lo = _np.clip(starts - half, 0, total)
        hi = _np.clip(starts - half + window, 0, total)
        counts = _np.maximum(hi - lo, 1)
        rms = _np.sqrt((cumulative[hi] - cumulative[lo]) / counts)
        with _np.errstate(divide="ignore"):
            db = 20.0 * _np.log10(_np.maximum(rms, 1e-12))
        db = _np.maximum(db, SILENCE_FLOOR_DB)
        return Envelope([float(v) for v in db], hop / sample_rate, duration)

    values: list[float] = []
    half = window // 2
    for start in range(0, total, hop):
        lo = max(0, start - half)
        hi = min(total, lo + window)
        if hi <= lo:
            values.append(SILENCE_FLOOR_DB)
            continue
        acc = 0.0
        for i in range(lo, hi):
            acc += samples[i] * samples[i]
        rms = math.sqrt(acc / (hi - lo))
        values.append(max(SILENCE_FLOOR_DB, 20.0 * math.log10(max(rms, 1e-12))))
    return Envelope(values, hop / sample_rate, duration)


def percentile(values: list[float], pct: float) -> float:
    """0..100 백분위수 (선형 보간)."""
    if not values:
        return SILENCE_FLOOR_DB
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * max(0.0, min(100.0, pct)) / 100.0
    low = int(math.floor(position))
    high = min(low + 1, len(ordered) - 1)
    weight = position - low
    return ordered[low] * (1 - weight) + ordered[high] * weight


def estimate_noise_floor(envelope: Envelope) -> float:
    """하위 백분위수를 배경 잡음 레벨로 추정한다."""
    return percentile(envelope.values_db, 12.0)


def estimate_speech_level(envelope: Envelope) -> float:
    """상위 구간을 실제 발화 레벨로 추정한다."""
    return percentile(envelope.values_db, 88.0)
