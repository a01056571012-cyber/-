import pytest

from precut.script import (
    ScriptEntry,
    group_by_source,
    parse_script,
    read_script,
    render_script,
    write_script,
)
from precut.subtitles import regions_from_transcript
from precut.transcribe import Transcript, Utterance


def entries():
    return [
        ScriptEntry(1, "영상1.mp4", 3.0, 8.5, "안녕하세요 홍유미입니다"),
        ScriptEntry(2, "영상1.mp4", 12.0, 15.25, "오늘은 숲에 왔습니다"),
        ScriptEntry(3, "영상 2.mp4", 1.5, 4.0, "여기가 입구예요"),
    ]


def test_render_includes_guidance_and_every_entry():
    text = render_script(entries())
    assert "- 를 붙이거나" in text
    assert text.count("[00") == 3
    assert "안녕하세요 홍유미입니다" in text
    assert "영상 2.mp4" in text


def test_round_trip_keeps_times():
    parsed = parse_script(render_script(entries()))
    assert len(parsed) == 3
    assert parsed[0].start == pytest.approx(3.0)
    assert parsed[0].end == pytest.approx(8.5)
    assert parsed[1].end == pytest.approx(15.25, abs=0.01)
    assert parsed[2].source == "영상 2.mp4"
    assert parsed[0].text == "안녕하세요 홍유미입니다"


def test_lines_marked_with_dash_are_dropped():
    text = render_script(entries())
    lines = text.splitlines()
    for position, line in enumerate(lines):
        if line.startswith("[002]"):
            lines[position] = "-" + line
    parsed = parse_script("\n".join(lines))
    assert [entry.index for entry in parsed] == [1, 3]


def test_deleted_lines_are_simply_gone():
    lines = [line for line in render_script(entries()).splitlines() if not line.startswith("[001]")]
    parsed = parse_script("\n".join(lines))
    assert [entry.index for entry in parsed] == [2, 3]


def test_hand_edited_times_are_respected():
    parsed = parse_script("[001] | a.mp4 | 00:00:10.00 - 00:00:20.00 | 10.0초 | 손으로 고침")
    assert parsed[0].start == pytest.approx(10.0)
    assert parsed[0].end == pytest.approx(20.0)


def test_broken_and_reversed_lines_are_skipped():
    text = "\n".join([
        "그냥 메모 줄",
        "[002] | a.mp4 | 00:00:20.00 - 00:00:10.00 | 잘못됨 |",
        "[003] | a.mp4 | 00:00:01.00 - 00:00:02.00 | 1.0초 | 정상",
    ])
    parsed = parse_script(text)
    assert [entry.index for entry in parsed] == [3]


def test_group_by_source_sorts_spans():
    grouped = group_by_source([
        ScriptEntry(1, "a.mp4", 9.0, 10.0, ""),
        ScriptEntry(2, "a.mp4", 1.0, 2.0, ""),
        ScriptEntry(3, "b.mp4", 5.0, 6.0, ""),
    ])
    assert grouped["a.mp4"] == [(1.0, 2.0), (9.0, 10.0)]
    assert grouped["b.mp4"] == [(5.0, 6.0)]


def test_write_and_read_file(tmp_path):
    path = write_script(entries(), tmp_path / "대본.txt")
    assert path.exists()
    assert len(read_script(path)) == 3


def test_empty_script_is_rejected(tmp_path):
    path = tmp_path / "빈대본.txt"
    path.write_text("# 전부 지웠습니다\n", encoding="utf-8")
    with pytest.raises(ValueError, match="찾지 못했습니다"):
        read_script(path)


def test_sentence_regions_follow_utterances():
    transcript = Transcript(
        utterances=[
            Utterance(start=2.0, end=5.0, text="안녕하세요"),
            Utterance(start=9.0, end=12.0, text="시작하겠습니다"),
            Utterance(start=13.0, end=14.0, text="   "),
        ],
        language="ko",
    )
    regions = regions_from_transcript(transcript, duration=20.0, lead_in=0.5, lead_out=0.5)
    assert regions == [(1.5, 5.5), (8.5, 12.5)]


def test_sentence_regions_are_clamped_to_media():
    transcript = Transcript(utterances=[Utterance(start=0.1, end=9.9, text="말")], language="ko")
    regions = regions_from_transcript(transcript, duration=10.0, lead_in=1.0, lead_out=1.0)
    assert regions == [(0.0, 10.0)]
