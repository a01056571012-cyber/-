"""실제 ffmpeg로 만든 샘플 미디어를 처리하는 통합 테스트."""

from __future__ import annotations

import json
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from precut.cli import main
from precut.config import build_settings
from precut.media import MediaError, find_ffmpeg, probe
from precut.pipeline import _transcribe_reporter, run_pipeline
from precut.script import read_script
from precut.transcribe import Transcript, TranscriptionUnavailable, Utterance, Word

# 말 2초 / 무음 2초를 반복하고, 6초 이후에는 목소리가 작아지는 12초짜리 샘플
SPEECH_EXPR = (
    "(0.30*lt(t\\,6)+0.06*gte(t\\,6))"
    "*sin(2*PI*220*t)*between(mod(t\\,4)\\,0\\,2)"
)


@pytest.fixture(scope="session")
def ffmpeg() -> str:
    try:
        return find_ffmpeg()
    except MediaError:  # pragma: no cover - ffmpeg 없는 환경
        pytest.skip("ffmpeg를 사용할 수 없습니다")


@pytest.fixture(scope="session")
def sample_video(tmp_path_factory, ffmpeg) -> Path:
    path = tmp_path_factory.mktemp("media") / "sample.mp4"
    subprocess.run(
        [
            ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=30:duration=12",
            "-f", "lavfi", "-i", f"aevalsrc='{SPEECH_EXPR}':d=12:s=44100:c=stereo",
            "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-shortest", str(path),
        ],
        check=True,
        capture_output=True,
    )
    return path


def base_settings(tmp_path: Path):
    settings = build_settings("talking-head")
    settings.make_subtitles = False
    settings.output.outdir = tmp_path / "out"
    return settings


def test_probe_reads_media_basics(sample_video):
    info = probe(sample_video)
    assert info.has_video and info.has_audio
    assert info.duration == pytest.approx(12.0, abs=0.3)
    assert info.frame_rate.timebase == 30
    assert (info.width, info.height) == (320, 240)


def test_pipeline_finds_the_speech_bursts(sample_video, tmp_path):
    result = run_pipeline(sample_video, base_settings(tmp_path))
    assert len(result.plan.segments) == 3
    for segment, expected_start in zip(result.plan.segments, (0.0, 4.0, 8.0)):
        assert segment.source_in == pytest.approx(expected_start, abs=0.3)
        assert segment.duration == pytest.approx(2.0, abs=0.5)
    # 무음 6초 중 앞뒤 여유(lead-in/out)를 남기고 약 5초가 잘려나간다.
    assert result.plan.removed_duration == pytest.approx(5.0, abs=0.7)


def test_pipeline_boosts_the_quiet_segment(sample_video, tmp_path):
    result = run_pipeline(sample_video, base_settings(tmp_path))
    gains = [segment.gain_db for segment in result.plan.segments]
    # 6초 이후 구간(-14dB 작게 녹음)이 앞 구간보다 많이 증폭되어야 한다.
    assert gains[-1] > gains[0]
    assert gains[-1] > 2.0
    assert result.program_lufs is not None


def test_pipeline_writes_expected_files(sample_video, tmp_path):
    settings = base_settings(tmp_path)
    settings.output.write_edl = True
    result = run_pipeline(sample_video, settings)
    for kind in ("xml", "edl", "jsx", "report"):
        assert result.outputs[kind].exists(), kind
    report = json.loads(result.outputs["report"].read_text(encoding="utf-8"))
    assert report["cuts"] == len(result.plan.segments)
    assert report["removed_duration"] > 0
    assert "<!DOCTYPE xmeml>" in result.outputs["xml"].read_text(encoding="utf-8")


def test_workdir_is_cleaned_up(sample_video, tmp_path):
    settings = base_settings(tmp_path)
    run_pipeline(sample_video, settings)
    leftovers = list((tmp_path / "out").glob("precut-*"))
    assert leftovers == []


def test_threshold_override_changes_the_cut(sample_video, tmp_path):
    settings = base_settings(tmp_path)
    settings.detect.threshold_db = -95.0  # 디지털 무음까지 발화로 취급
    result = run_pipeline(sample_video, settings)
    assert len(result.plan.segments) == 1
    assert result.plan.removed_duration < 1.0


def test_render_preview_produces_a_shorter_video(sample_video, tmp_path):
    settings = base_settings(tmp_path)
    settings.output.render_preview = True
    result = run_pipeline(sample_video, settings)
    preview = result.outputs["preview"]
    assert preview.exists() and preview.stat().st_size > 0
    info = probe(preview)
    assert info.duration == pytest.approx(result.plan.kept_duration, abs=0.5)
    assert info.duration < 9.0


def test_render_audio_matches_cut_length(sample_video, tmp_path):
    settings = base_settings(tmp_path)
    settings.output.render_audio = True
    result = run_pipeline(sample_video, settings)
    audio = result.outputs["audio"]
    assert audio.suffix == ".wav"
    assert probe(audio).duration == pytest.approx(result.plan.kept_duration, abs=0.5)


def test_audio_only_input_is_supported(sample_video, tmp_path, ffmpeg):
    wav = tmp_path / "voice.wav"
    subprocess.run(
        [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", str(sample_video),
         "-vn", "-c:a", "pcm_s16le", str(wav)],
        check=True,
        capture_output=True,
    )
    result = run_pipeline(wav, base_settings(tmp_path))
    assert len(result.plan.segments) == 3
    root = ET.fromstring(result.outputs["xml"].read_text(encoding="utf-8"))
    assert root.findall("sequence/media/video/track/clipitem") == []
    # 스테레오 2트랙 × 컷 3개
    assert len(root.findall("sequence/media/audio/track/clipitem")) == 6


def test_cli_dry_run_writes_nothing(sample_video, tmp_path, capsys):
    outdir = tmp_path / "cli-out"
    code = main([str(sample_video), "--no-subtitles", "--dry-run", "-o", str(outdir), "-q"])
    assert code == 0
    assert list(outdir.glob("*")) == []
    assert "컷" in capsys.readouterr().out


def test_cli_json_output(sample_video, tmp_path, capsys):
    code = main([str(sample_video), "--no-subtitles", "--json", "-o", str(tmp_path / "j")])
    assert code == 0
    report = json.loads(capsys.readouterr().out)
    assert report["cuts"] == 3
    assert Path(report["outputs"]["xml"]).exists()


def test_cli_reports_missing_file():
    assert main(["없는파일.mp4", "--no-subtitles"]) == 1


def fake_transcript() -> Transcript:
    """STT 백엔드 없이 자막 단계를 검증하기 위한 가짜 인식 결과."""
    pairs = [
        (0.4, 0.9, "안녕하세요"), (1.0, 1.6, "반갑습니다."),
        (2.6, 3.2, "여기는"), (3.3, 3.8, "잘려나갑니다"),   # 무음 구간이라 사라져야 한다
        (4.4, 5.0, "다시"), (5.1, 5.7, "이어서"), (5.8, 6.4, "말합니다."),
        (8.3, 8.9, "마지막"), (9.0, 9.6, "문장입니다."),
    ]
    words = [Word(start=s, end=e, text=t) for s, e, t in pairs]
    utterance = Utterance(start=words[0].start, end=words[-1].end,
                          text=" ".join(w.text for w in words), words=words)
    return Transcript(utterances=[utterance], language="ko", backend="fake")


def test_subtitles_follow_the_cut_timeline(sample_video, tmp_path):
    settings = base_settings(tmp_path)
    settings.make_subtitles = True
    result = run_pipeline(sample_video, settings, transcript=fake_transcript())

    text = result.outputs["srt"].read_text(encoding="utf-8")
    assert "잘려나갑니다" not in text
    assert "안녕하세요" in text and "마지막" in text
    assert result.outputs["srt"].suffix == ".srt"

    # 모든 자막은 컷 편집된 길이 안에 들어와야 한다.
    for cue in result.cues:
        assert 0 <= cue.start < cue.end <= result.plan.kept_duration + 1e-6
    # 원본 8.3초의 말은 앞의 무음이 잘린 만큼 당겨진다.
    assert result.cues[-1].start < 8.0


def test_subtitle_backend_missing_is_only_a_warning(sample_video, tmp_path, monkeypatch):
    monkeypatch.setattr("precut.pipeline.transcribe", _raise_unavailable)
    settings = base_settings(tmp_path)
    settings.make_subtitles = True
    result = run_pipeline(sample_video, settings)
    assert result.cues == []
    assert any("faster-whisper" in warning for warning in result.warnings)
    assert result.outputs["xml"].exists()  # 컷 결과는 그대로 나온다


def _raise_unavailable(*args, **kwargs):
    raise TranscriptionUnavailable("자막 생성을 위해 faster-whisper 가 필요합니다.")


@pytest.fixture(scope="session")
def quiet_video(tmp_path_factory, ffmpeg) -> Path:
    """앞의 샘플보다 전체적으로 작게 녹음된 8초짜리 두 번째 영상."""
    path = tmp_path_factory.mktemp("media2") / "second.mp4"
    expr = "0.05*sin(2*PI*220*t)*between(mod(t\\,4)\\,0\\,2)"
    subprocess.run(
        [
            ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=30:duration=8",
            "-f", "lavfi", "-i", f"aevalsrc='{expr}':d=8:s=44100:c=stereo",
            "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-shortest", str(path),
        ],
        check=True,
        capture_output=True,
    )
    return path


def test_merge_puts_every_clip_in_one_sequence(sample_video, quiet_video, tmp_path):
    settings = base_settings(tmp_path)
    result = run_pipeline([sample_video, quiet_video], settings, merge=True)

    assert result.timeline.is_merged
    assert len(result.timeline.items) == 2
    # 12초 영상에서 3컷 + 8초 영상에서 2컷
    assert len(result.timeline.place()) == 5
    assert result.timeline.source_duration == pytest.approx(20.0, abs=0.5)

    root = ET.fromstring(result.outputs["xml"].read_text(encoding="utf-8"))
    clips = root.findall("sequence/media/video/track/clipitem")
    assert len(clips) == 5
    assert [c.findtext("name") for c in clips[:3]] == ["sample.mp4"] * 3
    assert [c.findtext("name") for c in clips[3:]] == ["second.mp4"] * 2

    cursor = 0
    for clip in clips:
        assert int(clip.findtext("start")) == cursor
        cursor = int(clip.findtext("end"))
    assert int(root.find("sequence").findtext("duration")) == cursor


def test_merge_balances_volume_across_videos(sample_video, quiet_video, tmp_path):
    settings = base_settings(tmp_path)
    result = run_pipeline([sample_video, quiet_video], settings, merge=True)

    placed = result.timeline.place()
    # 첫 영상의 앞 두 컷은 크게, 두 번째 영상은 통째로 작게 녹음되어 있다.
    loud = [c.segment.gain_db for c in placed if c.item_index == 0][:2]
    quiet = [c.segment.gain_db for c in placed if c.item_index == 1]
    assert min(quiet) - max(loud) > 5.0

    # 보정 뒤에는 컷들 사이의 음량 편차가 줄어야 한다.
    before = [c.segment.loudness_lufs for c in placed]
    after = [c.segment.loudness_lufs + c.segment.gain_db for c in placed]
    assert max(after) - min(after) < max(before) - min(before)


def test_merge_writes_a_single_output_set(sample_video, quiet_video, tmp_path):
    settings = base_settings(tmp_path)
    result = run_pipeline([sample_video, quiet_video], settings, merge=True)
    assert len(list((tmp_path / "out").glob("*.xml"))) == 1
    report = json.loads(result.outputs["report"].read_text(encoding="utf-8"))
    assert report["merged"] is True
    assert len(report["sources"]) == 2
    assert {segment["source"] for segment in report["segments"]} == {"sample.mp4", "second.mp4"}


def test_merged_subtitles_are_offset_per_video(sample_video, quiet_video, tmp_path):
    settings = base_settings(tmp_path)
    settings.make_subtitles = True
    result = run_pipeline(
        [sample_video, quiet_video], settings, merge=True, transcript=fake_transcript()
    )
    assert result.cues
    for cue in result.cues:
        assert 0 <= cue.start < cue.end <= result.timeline.duration + 1e-6
    for left, right in zip(result.cues, result.cues[1:]):
        assert left.start <= right.start
    assert [cue.index for cue in result.cues] == list(range(1, len(result.cues) + 1))
    # 두 번째 영상의 자막은 첫 영상이 끝난 뒤에 나온다.
    first_video_end = result.timeline.place()[2].end_frame / result.timeline.frame_rate.fps
    assert max(cue.start for cue in result.cues) > first_video_end


def test_merge_skips_preview_rendering_with_a_warning(sample_video, quiet_video, tmp_path):
    settings = base_settings(tmp_path)
    settings.output.render_preview = True
    result = run_pipeline([sample_video, quiet_video], settings, merge=True)
    assert "preview" not in result.outputs
    assert any("이어붙일 때는" in warning for warning in result.warnings)
    assert result.outputs["xml"].exists()


def test_cli_merge_flag_produces_one_sequence(sample_video, quiet_video, tmp_path, capsys):
    outdir = tmp_path / "merged"
    code = main([str(sample_video), str(quiet_video), "--merge", "--no-subtitles",
                 "--json", "-o", str(outdir)])
    assert code == 0
    report = json.loads(capsys.readouterr().out)
    assert report["merged"] is True
    assert report["cuts"] == 5
    assert len(list(outdir.glob("*.xml"))) == 1


def test_cli_without_merge_keeps_files_separate(sample_video, quiet_video, tmp_path, capsys):
    outdir = tmp_path / "separate"
    code = main([str(sample_video), str(quiet_video), "--no-subtitles", "--json", "-o", str(outdir)])
    assert code == 0
    reports = json.loads(capsys.readouterr().out)
    assert len(reports) == 2
    assert all(report["merged"] is False for report in reports)
    assert len(list(outdir.glob("*.xml"))) == 2


def test_transcribe_reporter_throttles_updates():
    messages = []
    report = _transcribe_reporter("a.mp4", lambda stage, text: messages.append(text), step=25)
    for done in range(0, 101):
        report(float(done), 100.0)
    assert len(messages) == 4
    assert "25%" in messages[0]
    assert "100%" in messages[-1]
    assert "a.mp4" in messages[0]


def test_transcribe_reporter_ignores_unknown_duration():
    messages = []
    report = _transcribe_reporter("a.mp4", lambda stage, text: messages.append(text))
    report(5.0, 0.0)
    assert messages == []


def test_transcribe_reporter_shows_remaining_time(sample_video, tmp_path):
    messages = []
    report = _transcribe_reporter("a.mp4", lambda stage, text: messages.append(text), step=50)
    report(90.0, 180.0)
    assert "1:30.0 / 3:00.0" in messages[0]


def test_target_duration_shortens_the_result(sample_video, quiet_video, tmp_path):
    settings = base_settings(tmp_path)
    natural = run_pipeline([sample_video, quiet_video], settings, merge=True).timeline.duration

    settings = base_settings(tmp_path)
    settings.target_duration = natural * 0.5
    result = run_pipeline([sample_video, quiet_video], settings, merge=True)

    assert result.timeline.duration < natural
    assert result.timeline.duration <= natural * 0.75


def test_target_longer_than_possible_keeps_the_natural_cut(sample_video, tmp_path):
    settings = base_settings(tmp_path)
    natural = run_pipeline(sample_video, settings).timeline.duration

    settings = base_settings(tmp_path)
    settings.target_duration = 3600.0
    result = run_pipeline(sample_video, settings)
    assert result.timeline.duration == pytest.approx(natural, abs=0.5)


def test_cli_accepts_target_duration(sample_video, tmp_path, capsys):
    code = main([str(sample_video), "--no-subtitles", "-t", "4s", "--json",
                 "-o", str(tmp_path / "t")])
    assert code == 0
    report = json.loads(capsys.readouterr().out)
    assert report["kept_duration"] < 7.0


def test_keep_head_protects_the_opening(sample_video, tmp_path):
    settings = base_settings(tmp_path)
    settings.detect.threshold_db = -20.0  # 거의 다 무음으로 보게 해서 앞부분이 잘리도록
    without = run_pipeline(sample_video, settings)

    settings = base_settings(tmp_path)
    settings.detect.threshold_db = -20.0
    settings.shape.keep_head = 3.0
    with_head = run_pipeline(sample_video, settings)

    assert with_head.timeline.place()[0].segment.source_in == 0.0
    assert with_head.timeline.duration > without.timeline.duration


def test_keep_head_survives_target_duration(sample_video, quiet_video, tmp_path):
    settings = base_settings(tmp_path)
    settings.shape.keep_head = 2.0
    settings.shape.keep_tail = 1.0
    settings.target_duration = 3.0
    result = run_pipeline([sample_video, quiet_video], settings, merge=True)

    placed = result.timeline.place()
    assert placed[0].segment.source_in == 0.0
    assert placed[0].item_index == 0
    # 맨 끝 구간도 남아 있어야 한다
    assert placed[-1].item_index == len(result.timeline.items) - 1


def test_sentence_mode_cuts_on_utterance_boundaries(sample_video, tmp_path):
    settings = base_settings(tmp_path)
    settings.cut_by = "sentence"
    result = run_pipeline(sample_video, settings, transcript=fake_transcript())

    # 가짜 인식 결과의 말들이 있는 구간만 남는다
    for clip in result.timeline.place():
        assert clip.segment.duration > 0
    assert result.timeline.duration < result.timeline.source_duration


def test_script_file_is_written_and_reusable(sample_video, tmp_path):
    settings = base_settings(tmp_path)
    first = run_pipeline(sample_video, settings)
    script = first.outputs["script"]
    assert script.exists()

    entries = read_script(script)
    assert len(entries) == len(first.timeline.place())

    # 첫 줄만 남기고 지운 대본으로 다시 만들면 그 구간만 남는다
    kept = [line for line in script.read_text(encoding="utf-8").splitlines()
            if not line.startswith("[") or line.startswith("[001]")]
    trimmed = tmp_path / "고친대본.txt"
    trimmed.write_text("\n".join(kept), encoding="utf-8")

    settings = base_settings(tmp_path)
    settings.output.outdir = tmp_path / "out2"
    settings.script_path = trimmed
    second = run_pipeline(sample_video, settings)

    assert len(second.timeline.place()) == 1
    assert second.timeline.duration == pytest.approx(entries[0].duration, abs=0.1)


def test_script_with_unknown_file_is_reported(sample_video, tmp_path):
    script = tmp_path / "대본.txt"
    script.write_text(
        "[001] | 없는영상.mp4 | 00:00:01.00 - 00:00:03.00 | 2.0초 | 메모\n"
        "[002] | sample.mp4 | 00:00:01.00 - 00:00:04.00 | 3.0초 | 남김\n",
        encoding="utf-8",
    )
    settings = base_settings(tmp_path)
    settings.script_path = script
    result = run_pipeline(sample_video, settings)

    assert any("처리 대상에 없는 파일" in warning for warning in result.warnings)
    assert result.timeline.duration == pytest.approx(3.0, abs=0.1)
