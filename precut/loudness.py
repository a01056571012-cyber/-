"""EBU R128 기반 라우드니스 측정과 구간별 소리 밸런스 계산."""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from pathlib import Path

from .media import find_ffmpeg, run
from .segments import CutPlan, Segment

ABSOLUTE_GATE_LUFS = -70.0
RELATIVE_GATE_LU = -10.0

_EBUR128_LINE = re.compile(
    r"t:\s*(?P<t>[-\d.]+)\s+TARGET:\s*-?\d+\s+LUFS\s+"
    r"M:\s*(?P<m>[-\w.]+)\s+S:\s*(?P<s>[-\w.]+)"
)


@dataclass(frozen=True)
class LoudnessSample:
    time: float
    momentary: float
    short_term: float


@dataclass
class LoudnessTrack:
    """시간축을 따라 측정한 순간 라우드니스."""

    samples: list[LoudnessSample]

    def in_range(self, start: float, end: float) -> list[float]:
        # 순간 라우드니스는 400ms 창의 '끝'에 기록되므로 절반만큼 되돌려 맞춘다.
        return [s.momentary for s in self.samples if start <= (s.time - 0.2) < end]

    def segment_loudness(self, start: float, end: float) -> float | None:
        return gated_loudness(self.in_range(start, end))

    def program_loudness(self) -> float | None:
        return gated_loudness([s.momentary for s in self.samples])


def _to_float(text: str) -> float:
    lowered = text.strip().lower()
    if lowered in ("-inf", "inf", "nan", "-nan"):
        return float("-inf")
    try:
        return float(text)
    except ValueError:
        return float("-inf")


def parse_ebur128_log(text: str) -> LoudnessTrack:
    """`ffmpeg -af ebur128` 로그에서 시간별 라우드니스를 뽑는다."""
    samples: list[LoudnessSample] = []
    for match in _EBUR128_LINE.finditer(text):
        samples.append(
            LoudnessSample(
                time=float(match.group("t")),
                momentary=_to_float(match.group("m")),
                short_term=_to_float(match.group("s")),
            )
        )
    return LoudnessTrack(samples)


def gated_loudness(values: list[float]) -> float | None:
    """절대/상대 게이팅을 적용한 에너지 평균 (BS.1770 방식의 축약판)."""
    usable = [v for v in values if v > ABSOLUTE_GATE_LUFS and math.isfinite(v)]
    if not usable:
        return None
    ungated = _energy_mean(usable)
    gated = [v for v in usable if v > ungated + RELATIVE_GATE_LU]
    return _energy_mean(gated or usable)


def _energy_mean(values: list[float]) -> float:
    energy = sum(10.0 ** (v / 10.0) for v in values) / len(values)
    return 10.0 * math.log10(max(energy, 1e-12))


def measure_track(path: Path, *, ffmpeg: str | None = None) -> LoudnessTrack:
    """파일 전체를 한 번 통과시키며 순간 라우드니스를 수집한다."""
    ffmpeg = ffmpeg or find_ffmpeg()
    # ebur128에 metadata=1을 주면 프레임별 로그 대신 메타데이터만 남기므로 쓰지 않는다.
    proc = run(
        [
            ffmpeg, "-hide_banner", "-nostats", "-loglevel", "info", "-i", str(path),
            "-map", "0:a:0", "-af", "ebur128",
            "-f", "null", "-",
        ],
        check=False,
    )
    track = parse_ebur128_log((proc.stderr or "") + (proc.stdout or ""))
    if not track.samples and proc.returncode != 0:
        tail = "\n".join((proc.stderr or "").strip().splitlines()[-8:])
        raise RuntimeError(f"라우드니스 측정 실패:\n{tail}")
    return track


@dataclass
class LoudnormStats:
    input_i: float
    input_tp: float
    input_lra: float
    input_thresh: float
    target_offset: float

    def to_filter(self, *, target_i: float, target_tp: float, target_lra: float) -> str:
        return (
            f"loudnorm=I={target_i}:TP={target_tp}:LRA={target_lra}"
            f":measured_I={self.input_i}:measured_TP={self.input_tp}"
            f":measured_LRA={self.input_lra}:measured_thresh={self.input_thresh}"
            f":offset={self.target_offset}:linear=true:print_format=summary"
        )


def parse_loudnorm_json(text: str) -> LoudnormStats | None:
    """loudnorm 1차 패스가 출력한 JSON 블록을 찾는다."""
    start = text.rfind("{")
    while start != -1:
        depth = 0
        for index in range(start, len(text)):
            if text[index] == "{":
                depth += 1
            elif text[index] == "}":
                depth -= 1
                if depth == 0:
                    try:
                        data = json.loads(text[start : index + 1])
                    except json.JSONDecodeError:
                        break
                    if "input_i" in data:
                        return LoudnormStats(
                            input_i=float(data["input_i"]),
                            input_tp=float(data["input_tp"]),
                            input_lra=float(data["input_lra"]),
                            input_thresh=float(data["input_thresh"]),
                            target_offset=float(data.get("target_offset", 0.0)),
                        )
                    break
        start = text.rfind("{", 0, start)
    return None


def measure_loudnorm(path: Path, *, target_i: float, target_tp: float, target_lra: float,
                     ffmpeg: str | None = None, extra_filters: str = "") -> LoudnormStats | None:
    """loudnorm 2패스 정규화를 위한 1차 측정."""
    ffmpeg = ffmpeg or find_ffmpeg()
    chain = f"{extra_filters}," if extra_filters else ""
    proc = run(
        [
            ffmpeg, "-hide_banner", "-nostats", "-i", str(path),
            "-map", "0:a:0",
            "-af", f"{chain}loudnorm=I={target_i}:TP={target_tp}:LRA={target_lra}:print_format=json",
            "-f", "null", "-",
        ],
        check=False,
    )
    return parse_loudnorm_json((proc.stderr or "") + (proc.stdout or ""))


@dataclass
class BalanceOptions:
    """구간별 볼륨 균형 규칙."""

    enabled: bool = True
    target_lufs: float = -16.0
    strength: float = 0.75
    max_boost_db: float = 9.0
    max_cut_db: float = 9.0
    reference: str = "program"  # program | target


def weighted_median(pairs: list[tuple[float, float]]) -> float | None:
    """(값, 가중치) 목록의 가중 중앙값. 길이가 긴 컷에 더 큰 비중을 준다.

    누적분포를 선형 보간해서 구한다. 큰 구간과 작은 구간이 반반이면 어느 한쪽으로
    치우치지 않고 그 중간을 기준으로 삼아야 양쪽이 대칭적으로 보정되기 때문이다.
    """
    usable = [(value, weight) for value, weight in pairs if math.isfinite(value) and weight > 0]
    if not usable:
        return None
    usable.sort()
    total = sum(weight for _, weight in usable)

    points: list[tuple[float, float]] = []
    cumulative = 0.0
    for value, weight in usable:
        points.append(((cumulative + weight / 2.0) / total, value))
        cumulative += weight

    if 0.5 <= points[0][0]:
        return points[0][1]
    for (low_pos, low_value), (high_pos, high_value) in zip(points, points[1:]):
        if low_pos <= 0.5 <= high_pos:
            if high_pos - low_pos < 1e-12:
                return (low_value + high_value) / 2.0
            ratio = (0.5 - low_pos) / (high_pos - low_pos)
            return low_value + ratio * (high_value - low_value)
    return points[-1][1]


def apply_segment_balance(
    plan: CutPlan, track: LoudnessTrack, options: BalanceOptions
) -> tuple[list[Segment], float | None]:
    """각 컷의 라우드니스를 재서 gain_db를 채운다.

    기준(reference)이 'program'이면 컷들의 (길이 가중) 중앙값에 맞추므로 너무 작은
    구간은 올리고 너무 큰 구간은 내려 상대적인 균형이 잡힌다. 'target'이면 절대
    목표 LUFS로 맞춘다. 중앙값을 쓰는 이유는 에너지 평균이 큰 소리에 끌려가서
    작게 말한 구간이 그대로 남는 문제를 피하기 위해서다.
    """
    program = track.program_loudness()
    if not options.enabled:
        return plan.segments, program

    for segment in plan.segments:
        segment.loudness_lufs = track.segment_loudness(segment.source_in, segment.source_out)

    reference = options.target_lufs
    if options.reference == "program":
        median = weighted_median(
            [(seg.loudness_lufs, seg.duration) for seg in plan.segments if seg.loudness_lufs is not None]
        )
        if median is not None:
            reference = median

    strength = max(0.0, min(1.0, options.strength))
    for segment in plan.segments:
        measured = segment.loudness_lufs
        if measured is None or not math.isfinite(measured):
            segment.gain_db = 0.0
            continue
        correction = (reference - measured) * strength
        segment.gain_db = round(
            max(-options.max_cut_db, min(options.max_boost_db, correction)), 2
        )
    return plan.segments, program


def db_to_linear(db: float) -> float:
    return 10.0 ** (db / 20.0)
