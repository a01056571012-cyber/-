import pytest

from precut.segments import ShapeOptions, shape_regions
from precut.subtitles import (
    SubtitleOptions,
    build_cues,
    ends_sentence,
    render_srt,
    wrap_lines,
)
from precut.timecode import FrameRate
from precut.transcribe import Transcript, Utterance, Word

RATE = FrameRate(30)


def make_plan(regions, duration):
    options = ShapeOptions(lead_in=0.0, lead_out=0.0, min_silence=0.3, min_clip=0.1)
    return shape_regions(regions, duration=duration, options=options, frame_rate=RATE)


def words_from(pairs):
    return [Word(start=s, end=e, text=t) for s, e, t in pairs]


def transcript_from(pairs):
    words = words_from(pairs)
    utterance = Utterance(start=words[0].start, end=words[-1].end,
                          text=" ".join(w.text for w in words), words=words)
    return Transcript(utterances=[utterance], language="ko", backend="test")


def test_wrap_lines_keeps_short_text_on_one_line():
    assert wrap_lines("안녕하세요 반갑습니다", 20, 2) == ["안녕하세요 반갑습니다"]


def test_wrap_lines_balances_two_lines():
    text = "오늘은 카메라 세팅에 대해서 이야기해 보겠습니다"
    lines = wrap_lines(text, 16, 2)
    assert len(lines) == 2
    assert all(len(line) <= 16 for line in lines)
    assert abs(len(lines[0]) - len(lines[1])) <= 6
    assert " ".join(lines) == text


def test_wrap_lines_hard_splits_overlong_token():
    lines = wrap_lines("가" * 45, 20, 2)
    assert all(len(line) <= 40 for line in lines)
    assert "".join(lines).replace(" ", "") == "가" * 45


def test_ends_sentence_handles_korean_endings():
    assert ends_sentence("갑니다.")
    assert ends_sentence("좋아요")
    assert ends_sentence("했습니다")
    assert not ends_sentence("그리고")


def test_cues_are_placed_on_the_cut_timeline():
    plan = make_plan([(2.0, 4.0), (8.0, 10.0)], 12.0)
    transcript = transcript_from([
        (2.1, 2.6, "안녕하세요"),
        (2.7, 3.4, "반갑습니다"),
        (8.1, 8.9, "시작할게요"),
    ])
    cues = build_cues(transcript, plan.time_map(), SubtitleOptions(),
                      timeline_duration=plan.kept_duration)
    assert cues[0].start == pytest.approx(0.1, abs=0.05)
    # 원본 8.1초의 말은 컷 후 2.1초로 당겨진다.
    assert cues[-1].start == pytest.approx(2.1, abs=0.05)


def test_words_inside_removed_silence_are_dropped():
    plan = make_plan([(2.0, 4.0), (8.0, 10.0)], 12.0)
    transcript = transcript_from([
        (2.1, 2.6, "살아남는말"),
        (5.5, 6.0, "잘려나갈말"),
        (8.2, 8.7, "다음말"),
    ])
    cues = build_cues(transcript, plan.time_map(), SubtitleOptions(),
                      timeline_duration=plan.kept_duration)
    assert "잘려나갈말" not in "\n".join(cue.text for cue in cues)
    assert "살아남는말" in "\n".join(cue.text for cue in cues)


def test_long_pause_splits_cues():
    plan = make_plan([(0.0, 12.0)], 12.0)
    transcript = transcript_from([
        (0.5, 1.0, "첫번째"),
        (1.1, 1.6, "문장이고"),
        (4.0, 4.5, "두번째"),
        (4.6, 5.1, "문장"),
    ])
    options = SubtitleOptions(split_pause=0.5)
    cues = build_cues(transcript, plan.time_map(), options, timeline_duration=12.0)
    assert len(cues) == 2
    assert cues[0].text.startswith("첫번째")
    assert cues[1].text.startswith("두번째")


def test_sentence_end_splits_cues():
    plan = make_plan([(0.0, 12.0)], 12.0)
    transcript = transcript_from([
        (0.5, 1.0, "안녕하세요."),
        (1.05, 1.6, "오늘은"),
        (1.65, 2.2, "촬영입니다"),
    ])
    cues = build_cues(transcript, plan.time_map(), SubtitleOptions(), timeline_duration=12.0)
    assert len(cues) == 2


def test_cues_respect_min_duration_and_never_overlap():
    plan = make_plan([(0.0, 30.0)], 30.0)
    pairs = []
    for index in range(8):
        start = index * 1.2
        pairs.append((start, start + 0.15, f"말{index}"))
    transcript = transcript_from(pairs)
    options = SubtitleOptions(min_duration=1.0, min_gap=0.08, split_pause=0.3)
    cues = build_cues(transcript, plan.time_map(), options, timeline_duration=30.0)
    for cue in cues:
        assert cue.duration > 0
    for left, right in zip(cues, cues[1:]):
        assert left.end <= right.start - options.min_gap + 1e-6


def test_reading_speed_extends_short_cues():
    plan = make_plan([(0.0, 30.0)], 30.0)
    transcript = transcript_from([(1.0, 1.4, "아주긴자막" * 3)])
    options = SubtitleOptions(max_cps=5.0, max_duration=10.0)
    cues = build_cues(transcript, plan.time_map(), options, timeline_duration=30.0)
    assert cues[0].duration >= cues[0].char_count / 5.0 - 0.01


def test_cues_are_clipped_to_timeline_end():
    plan = make_plan([(0.0, 5.0)], 5.0)
    transcript = transcript_from([(4.6, 4.9, "끝말")])
    cues = build_cues(transcript, plan.time_map(), SubtitleOptions(),
                      timeline_duration=plan.kept_duration)
    assert cues[-1].end <= plan.kept_duration + 1e-6


def test_transcript_without_word_times_is_still_usable():
    utterance = Utterance(start=1.0, end=3.0, text="단어 시간이 없는 문장입니다", words=[])
    transcript = Transcript(utterances=[utterance], language="ko", backend="test")
    plan = make_plan([(0.0, 10.0)], 10.0)
    cues = build_cues(transcript, plan.time_map(), SubtitleOptions(), timeline_duration=10.0)
    assert cues and "단어" in cues[0].text


def test_render_srt_format():
    plan = make_plan([(0.0, 10.0)], 10.0)
    transcript = transcript_from([(1.0, 1.5, "안녕하세요")])
    cues = build_cues(transcript, plan.time_map(), SubtitleOptions(), timeline_duration=10.0)
    text = render_srt(cues)
    assert text.startswith("1\n00:00:01,000 --> ")
    assert "안녕하세요" in text
