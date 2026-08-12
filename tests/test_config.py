import json
from pathlib import Path

import pytest

from precut.cli import expand_inputs
from precut.config import PRESETS, build_settings


def test_default_preset_values():
    settings = build_settings("talking-head")
    assert settings.shape.lead_in == pytest.approx(0.12)
    assert settings.balance.enabled
    assert settings.xml.audio_transition_frames == 4


def test_presets_differ_meaningfully():
    tight = build_settings("tight")
    gentle = build_settings("gentle")
    assert tight.shape.min_silence < gentle.shape.min_silence
    assert tight.shape.lead_out < gentle.shape.lead_out


def test_every_preset_builds():
    for name in PRESETS:
        settings = build_settings(name)
        settings.shape.validate()


def test_unknown_preset_is_rejected():
    with pytest.raises(ValueError, match="프리셋"):
        build_settings("없는프리셋")


def test_config_file_overrides_preset(tmp_path: Path):
    config = tmp_path / "c.json"
    config.write_text(
        json.dumps({"shape": {"lead_in": 0.5}, "subtitles": {"max_cps": 8.0}}), encoding="utf-8"
    )
    settings = build_settings("talking-head", config)
    assert settings.shape.lead_in == 0.5
    assert settings.subtitles.max_cps == 8.0
    assert settings.shape.lead_out == pytest.approx(0.22)  # 나머지는 프리셋 그대로


def test_unknown_key_is_rejected(tmp_path: Path):
    config = tmp_path / "c.json"
    config.write_text(json.dumps({"shape": {"lead_inn": 0.5}}), encoding="utf-8")
    with pytest.raises(ValueError, match="lead_inn"):
        build_settings("talking-head", config)


def test_underscore_keys_are_treated_as_comments(tmp_path: Path):
    config = tmp_path / "c.json"
    config.write_text(json.dumps({"_설명": "메모", "shape": {"min_clip": 0.9}}), encoding="utf-8")
    assert build_settings("talking-head", config).shape.min_clip == 0.9


def test_example_config_is_valid():
    example = Path(__file__).resolve().parent.parent / "config.example.json"
    settings = build_settings("talking-head", example)
    assert settings.transcribe.model == "medium"
    assert settings.render.target_i == -16.0


def test_settings_serialize_to_json():
    payload = build_settings("vlog").to_dict()
    assert json.loads(json.dumps(payload, ensure_ascii=False))["shape"]["min_silence"] == 0.6


def test_folder_input_expands_to_media_files(tmp_path: Path):
    folder = tmp_path / "영상2"
    folder.mkdir()
    for name in ("a.mp4", "b.MOV", "c.wav", "메모.txt", "썸네일.jpg"):
        (folder / name).write_bytes(b"")
    (folder / "하위폴더").mkdir()

    expanded, problems = expand_inputs([folder])
    assert [p.name for p in expanded] == ["a.mp4", "b.MOV", "c.wav"]
    assert problems == []


def test_folder_without_media_is_reported(tmp_path: Path):
    folder = tmp_path / "빈폴더"
    folder.mkdir()
    (folder / "메모.txt").write_text("", encoding="utf-8")
    expanded, problems = expand_inputs([folder])
    assert expanded == []
    assert len(problems) == 1 and "찾지 못했습니다" in problems[0]


def test_file_inputs_pass_through_untouched(tmp_path: Path):
    video = tmp_path / "확장자없는영상"
    video.write_bytes(b"")
    expanded, problems = expand_inputs([video, tmp_path / "없는파일.mp4"])
    assert expanded == [video, tmp_path / "없는파일.mp4"]
    assert problems == []
