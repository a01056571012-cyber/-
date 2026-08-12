"""자막 큐 생성과 SRT/VTT 출력.

STT 결과는 '원본 시간축'이므로, 컷 편집 후 타임라인으로 옮기면서
잘려나간 구간의 단어를 버리고 읽기 좋은 길이로 다시 묶는다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from .segments import TimeMap
from .timecode import srt_timestamp, vtt_timestamp
from .transcribe import Transcript, Word

SENTENCE_END = tuple(".?!…。？！")
# 한국어 종결어미: 문장부호 없이 끝나는 자막을 자연스럽게 끊기 위한 힌트
KOREAN_ENDINGS = (
    "습니다", "합니다", "됩니다", "입니다", "니다", "세요", "예요", "에요",
    "어요", "아요", "지요", "네요", "군요", "거든요", "든요", "죠", "임", "함",
)


@dataclass
class SubtitleOptions:
    max_chars_per_line: int = 20
    max_lines: int = 2
    min_duration: float = 1.0
    max_duration: float = 6.0
    max_cps: float = 11.0
    min_gap: float = 0.08
    split_pause: float = 0.45
    lead_out: float = 0.15
    split_on_sentence: bool = True
    strip_fillers: bool = True
    fillers: tuple[str, ...] = ("음...", "어...", "그...", "음,", "어,")

    @property
    def max_chars(self) -> int:
        return self.max_chars_per_line * self.max_lines


@dataclass
class Cue:
    index: int
    start: float
    end: float
    lines: list[str] = field(default_factory=list)

    @property
    def text(self) -> str:
        return "\n".join(self.lines)

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)

    @property
    def char_count(self) -> int:
        return sum(len(line) for line in self.lines)


@dataclass
class _TimedToken:
    start: float
    end: float
    text: str
    gap_before: float = 0.0


def map_words_to_timeline(words: list[Word], time_map: TimeMap) -> list[_TimedToken]:
    """단어 타이밍을 컷 후 타임라인으로 옮기고, 잘려나간 단어는 버린다."""
    tokens: list[_TimedToken] = []
    for word in words:
        text = word.text.strip()
        if not text:
            continue
        pieces = time_map.split_span(word.start, max(word.end, word.start + 1e-3))
        if not pieces:
            continue
        tokens.append(_TimedToken(start=pieces[0][0], end=pieces[-1][1], text=text))

    for index in range(1, len(tokens)):
        tokens[index].gap_before = max(0.0, tokens[index].start - tokens[index - 1].end)
    return tokens


def _synthesize_word_times(transcript: Transcript) -> list[Word]:
    """단어 타임스탬프가 없을 때 문장을 글자 수 비례로 쪼갠다."""
    words: list[Word] = []
    for utterance in transcript.utterances:
        chunks = [c for c in re.split(r"(\s+)", utterance.text) if c.strip()]
        if not chunks:
            continue
        total = sum(len(c) for c in chunks) or 1
        cursor = utterance.start
        span = max(0.1, utterance.end - utterance.start)
        for chunk in chunks:
            length = span * len(chunk) / total
            words.append(Word(start=cursor, end=cursor + length, text=chunk))
            cursor += length
    return words


def ends_sentence(text: str) -> bool:
    stripped = text.rstrip("\"')]」』”")
    if stripped.endswith(SENTENCE_END):
        return True
    return any(stripped.endswith(ending) for ending in KOREAN_ENDINGS)


def build_cues(
    transcript: Transcript,
    time_map: TimeMap,
    options: SubtitleOptions,
    *,
    timeline_duration: float | None = None,
) -> list[Cue]:
    words = transcript.all_words() if transcript.has_word_times else _synthesize_word_times(transcript)
    tokens = map_words_to_timeline(words, time_map)
    if options.strip_fillers:
        tokens = [t for t in tokens if t.text not in options.fillers]
    if not tokens:
        return []

    groups: list[list[_TimedToken]] = []
    current: list[_TimedToken] = []

    def current_length(extra: str = "") -> int:
        base = sum(len(t.text) for t in current) + max(0, len(current) - 1)
        return base + (len(extra) + 1 if extra else 0)

    for token in tokens:
        if current:
            too_long = current_length(token.text) > options.max_chars
            too_slow = token.end - current[0].start > options.max_duration
            paused = token.gap_before >= options.split_pause
            sentence_break = options.split_on_sentence and ends_sentence(current[-1].text)
            if too_long or too_slow or paused or sentence_break:
                groups.append(current)
                current = []
        current.append(token)
    if current:
        groups.append(current)

    cues: list[Cue] = []
    for group in groups:
        text = " ".join(t.text for t in group).strip()
        if not text:
            continue
        lines = wrap_lines(text, options.max_chars_per_line, options.max_lines)
        cues.append(Cue(index=len(cues) + 1, start=group[0].start, end=group[-1].end, lines=lines))

    return _fix_timings(cues, options, timeline_duration)


def _fix_timings(cues: list[Cue], options: SubtitleOptions, timeline_duration: float | None) -> list[Cue]:
    """최소 노출 시간, 읽기 속도(CPS), 큐 간 간격을 보정한다."""
    for position, cue in enumerate(cues):
        cue.end += options.lead_out
        needed = max(options.min_duration, cue.char_count / max(1e-6, options.max_cps))
        target_end = cue.start + min(options.max_duration, max(needed, cue.duration))
        cue.end = max(cue.end, target_end)

        next_start = cues[position + 1].start if position + 1 < len(cues) else timeline_duration
        if next_start is not None:
            limit = next_start - options.min_gap
            if cue.end > limit:
                cue.end = max(cue.start + 0.2, limit)
        if timeline_duration is not None:
            cue.end = min(cue.end, timeline_duration)
        if cue.end <= cue.start:
            cue.end = cue.start + 0.2

    for position, cue in enumerate(cues):
        cue.index = position + 1
    return cues


def wrap_lines(text: str, max_chars: int, max_lines: int) -> list[str]:
    """단어 경계를 지키며 줄바꿈. 2줄이면 길이가 비슷하게 나누어 균형을 맞춘다."""
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return []
    if len(text) <= max_chars:
        return [text]

    words = text.split(" ")
    if max_lines == 2 and len(text) <= max_chars * 2:
        best_index, best_score = None, None
        for index in range(1, len(words)):
            first = " ".join(words[:index])
            second = " ".join(words[index:])
            if len(first) > max_chars or len(second) > max_chars:
                continue
            score = abs(len(first) - len(second))
            if best_score is None or score < best_score:
                best_index, best_score = index, score
        if best_index is not None:
            return [" ".join(words[:best_index]), " ".join(words[best_index:])]

    lines: list[str] = []
    current = ""
    for word in words:
        while len(word) > max_chars:
            if current:
                lines.append(current)
                current = ""
            lines.append(word[:max_chars])
            word = word[max_chars:]
        candidate = f"{current} {word}".strip()
        if current and len(candidate) > max_chars:
            lines.append(current)
            current = word
        else:
            current = candidate
    if current:
        lines.append(current)

    if len(lines) > max_lines:
        head = lines[: max_lines - 1]
        head.append(" ".join(lines[max_lines - 1 :]))
        lines = head
    return lines


def render_srt(cues: list[Cue]) -> str:
    blocks = []
    for cue in cues:
        blocks.append(
            f"{cue.index}\n{srt_timestamp(cue.start)} --> {srt_timestamp(cue.end)}\n{cue.text}\n"
        )
    return "\n".join(blocks)


def render_vtt(cues: list[Cue]) -> str:
    blocks = ["WEBVTT\n"]
    for cue in cues:
        blocks.append(
            f"{cue.index}\n{vtt_timestamp(cue.start)} --> {vtt_timestamp(cue.end)}\n{cue.text}\n"
        )
    return "\n".join(blocks)


def write_srt(cues: list[Cue], path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_srt(cues), encoding="utf-8")
    return path


def write_vtt(cues: list[Cue], path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_vtt(cues), encoding="utf-8")
    return path
