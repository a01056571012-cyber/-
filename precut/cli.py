"""명령줄 인터페이스."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from . import __version__
from .config import PRESETS, build_settings
from .media import MediaError
from .pipeline import run_pipeline
from .timecode import format_duration, parse_duration

EPILOG = """
예시:
  precut 인터뷰.mp4                       기본(토킹헤드) 설정으로 컷+밸런스+자막
  precut vlog.mp4 -p vlog -o ./편집본      프리셋과 출력 폴더 지정
  precut 강의.mp4 --no-subtitles          컷과 소리 밸런스만
  precut a.mp4 --render-preview           확인용 미리보기 영상까지 렌더링
  precut a.mp4 --dry-run                  파일을 만들지 않고 컷 결과만 미리보기
  precut ./영상폴더 --merge                폴더 안 영상을 하나의 시퀀스로 이어붙이기
  precut ./영상폴더 --merge -t 30m         목표 30분에 맞춰 자동으로 더 잘라내기

프리미어에서 열기:
  1) 만들어진 .xml 을 프리미어에서 파일 > 가져오기
  2) 프로젝트 패널에 생긴 시퀀스를 더블클릭
  3) .srt 을 가져와 타임라인 위로 끌어놓으면 캡션 트랙이 됩니다
  (또는 파일 > 스크립트 > 스크립트 파일 실행 에서 *_import.jsx 실행)
"""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="precut",
        description="프리미어 프로용 무음 컷편집 · 소리 밸런스 · 자막 자동화 도구",
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("inputs", nargs="*", type=Path, help="영상 또는 오디오 파일")
    parser.add_argument("-o", "--outdir", type=Path, help="결과물 폴더 (기본: <파일명>_precut)")
    parser.add_argument(
        "-p", "--preset", default="talking-head", choices=sorted(PRESETS), help="영상 유형 프리셋"
    )
    parser.add_argument("-c", "--config", type=Path, help="JSON 설정 파일")
    parser.add_argument(
        "-m", "--merge", action="store_true",
        help="여러 영상을 순서대로 이어붙여 하나의 시퀀스로 만듭니다",
    )
    parser.add_argument("--sequence-name", help="시퀀스 이름")
    parser.add_argument("--version", action="version", version=f"precut {__version__}")

    cut = parser.add_argument_group("컷 편집")
    cut.add_argument("--threshold-db", type=float, help="무음 판정 임계값(dBFS). 기본은 자동")
    cut.add_argument("--lead-in", type=float, help="말 시작 앞에 남길 여유(초)")
    cut.add_argument("--lead-out", type=float, help="말 끝 뒤에 남길 여유(초)")
    cut.add_argument("--min-silence", type=float, help="이보다 짧은 무음은 자르지 않음(초)")
    cut.add_argument("--min-clip", type=float, help="이보다 짧은 컷은 버림(초)")
    cut.add_argument("--keep-silence", type=float, help="무음을 완전히 없애지 않고 남길 길이(초)")
    cut.add_argument("--audio-transition", type=int, metavar="FRAMES", help="오디오 크로스페이드 프레임")
    cut.add_argument("--video-transition", type=int, metavar="FRAMES", help="영상 디졸브 프레임")
    cut.add_argument("--no-transitions", action="store_true", help="트랜지션 없이 하드컷")
    cut.add_argument(
        "-t", "--target-duration", metavar="시간",
        help="목표 길이에 맞춰 자동으로 더 잘라냄 (예: 30m, 1h20m, 1:30:00)",
    )

    balance = parser.add_argument_group("소리 밸런스")
    balance.add_argument("--no-balance", action="store_true", help="구간별 볼륨 보정 끄기")
    balance.add_argument("--balance-strength", type=float, help="보정 강도 0~1 (기본 0.75)")
    balance.add_argument("--balance-target", type=float, metavar="LUFS", help="목표 라우드니스")
    balance.add_argument(
        "--balance-reference", choices=("program", "target"),
        help="program=영상 평균에 맞춤, target=절대 목표값에 맞춤",
    )
    balance.add_argument("--max-boost", type=float, help="최대 증폭(dB)")
    balance.add_argument("--max-cut", type=float, help="최대 감쇠(dB)")

    subtitle = parser.add_argument_group("자막")
    subtitle.add_argument("--no-subtitles", action="store_true", help="자막 생성 건너뛰기")
    subtitle.add_argument("--model", help="Whisper 모델 (tiny/base/small/medium/large-v3)")
    subtitle.add_argument("--language", help="음성 언어 코드 (기본 ko, 자동감지는 auto)")
    subtitle.add_argument("--device", choices=("auto", "cpu", "cuda"), help="STT 실행 장치")
    subtitle.add_argument("--chars-per-line", type=int, help="한 줄 최대 글자 수")
    subtitle.add_argument("--max-lines", type=int, help="자막 최대 줄 수")
    subtitle.add_argument("--max-cps", type=float, help="초당 최대 글자 수(읽기 속도)")

    output = parser.add_argument_group("출력")
    output.add_argument("--edl", action="store_true", help="EDL도 함께 출력")
    output.add_argument("--vtt", action="store_true", help="WebVTT도 함께 출력")
    output.add_argument("--no-jsx", action="store_true", help="프리미어 스크립트 생성 안 함")
    output.add_argument("--render-preview", action="store_true", help="확인용 미리보기 영상 렌더링")
    output.add_argument("--render-audio", action="store_true", help="정규화된 오디오(WAV) 렌더링")
    output.add_argument("--burn-subtitles", action="store_true", help="미리보기에 자막 굽기")
    output.add_argument("--target-lufs", type=float, help="렌더링 목표 라우드니스(기본 -16)")
    output.add_argument("--dry-run", action="store_true", help="파일을 쓰지 않고 결과만 출력")
    output.add_argument("--json", action="store_true", help="결과를 JSON으로 출력")
    output.add_argument("-q", "--quiet", action="store_true", help="진행 메시지 숨김")

    return parser


def apply_args(settings, args) -> None:
    detect, shape = settings.detect, settings.shape
    if args.threshold_db is not None:
        detect.threshold_db = args.threshold_db
    for attribute, value in (
        ("lead_in", args.lead_in),
        ("lead_out", args.lead_out),
        ("min_silence", args.min_silence),
        ("min_clip", args.min_clip),
        ("max_silence_keep", args.keep_silence),
    ):
        if value is not None:
            setattr(shape, attribute, value)

    if args.no_transitions:
        settings.xml.audio_transition_frames = 0
        settings.xml.video_transition_frames = 0
    if args.audio_transition is not None:
        settings.xml.audio_transition_frames = args.audio_transition
    if args.video_transition is not None:
        settings.xml.video_transition_frames = args.video_transition

    balance = settings.balance
    if args.no_balance:
        balance.enabled = False
    if args.balance_strength is not None:
        balance.strength = args.balance_strength
    if args.balance_target is not None:
        balance.target_lufs = args.balance_target
    if args.balance_reference:
        balance.reference = args.balance_reference
    if args.max_boost is not None:
        balance.max_boost_db = args.max_boost
    if args.max_cut is not None:
        balance.max_cut_db = args.max_cut

    if args.no_subtitles:
        settings.make_subtitles = False
    if args.model:
        settings.transcribe.model = args.model
    if args.language:
        settings.transcribe.language = None if args.language == "auto" else args.language
    if args.device:
        settings.transcribe.device = args.device
    if args.chars_per_line is not None:
        settings.subtitles.max_chars_per_line = args.chars_per_line
    if args.max_lines is not None:
        settings.subtitles.max_lines = args.max_lines
    if args.max_cps is not None:
        settings.subtitles.max_cps = args.max_cps

    output = settings.output
    if args.outdir:
        output.outdir = args.outdir
    output.write_edl = args.edl
    output.write_vtt = args.vtt
    output.write_jsx = not args.no_jsx
    output.render_preview = args.render_preview
    output.render_audio = args.render_audio
    output.burn_subtitles = args.burn_subtitles
    if args.target_lufs is not None:
        settings.render.target_i = args.target_lufs
        settings.balance.target_lufs = args.target_lufs

    if args.target_duration:
        settings.target_duration = parse_duration(args.target_duration)

    if args.dry_run:
        output.write_xml = False
        output.write_edl = False
        output.write_jsx = False
        output.write_srt = False
        output.write_vtt = False
        output.write_report = False
        output.render_preview = False
        output.render_audio = False


MEDIA_SUFFIXES = frozenset(
    {
        ".mp4", ".mov", ".mkv", ".avi", ".m4v", ".mxf", ".webm", ".wmv",
        ".mts", ".m2ts", ".mpg", ".mpeg", ".flv", ".ts",
        ".wav", ".mp3", ".m4a", ".aac", ".flac", ".ogg", ".opus", ".wma",
    }
)


def natural_key(path: Path) -> list:
    """'영상2'가 '영상10'보다 앞에 오도록 숫자를 숫자로 비교한다."""
    parts = re.split(r"(\d+)", path.name.lower())
    return [int(part) if part.isdigit() else part for part in parts]


def expand_inputs(paths: list[Path]) -> tuple[list[Path], list[str]]:
    """폴더를 받으면 그 안의 미디어 파일로 펼친다."""
    expanded: list[Path] = []
    problems: list[str] = []
    for path in paths:
        if path.is_dir():
            found = sorted(
                (
                    child
                    for child in path.iterdir()
                    if child.is_file() and child.suffix.lower() in MEDIA_SUFFIXES
                ),
                key=natural_key,
            )
            if not found:
                problems.append(f"{path}: 폴더 안에서 영상·오디오 파일을 찾지 못했습니다")
            expanded.extend(found)
        else:
            expanded.append(path)
    return expanded, problems


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.inputs:
        parser.print_help()
        return 1

    def progress(stage: str, message: str) -> None:
        if not args.quiet and not args.json:
            print(f"  · {message}", file=sys.stderr, flush=True)

    inputs, problems = expand_inputs(args.inputs)
    for problem in problems:
        print(f"[오류] {problem}", file=sys.stderr)

    reports = []
    failures = len(problems)

    if args.merge and len(inputs) < 2:
        print("[알림] 이어붙일 영상이 하나뿐이라 그냥 처리합니다.", file=sys.stderr)

    batches = [inputs] if args.merge else [[path] for path in inputs]

    for batch in batches:
        try:
            settings = build_settings(args.preset, args.config)
            apply_args(settings, args)
            if not args.quiet and not args.json:
                if len(batch) > 1:
                    listing = "\n".join(f"   {index}. {p.name}" for index, p in enumerate(batch, 1))
                    print(f"\n▶ 영상 {len(batch)}개를 순서대로 이어붙입니다\n{listing}",
                          file=sys.stderr, flush=True)
                else:
                    print(f"\n▶ {batch[0]}", file=sys.stderr, flush=True)
            result = run_pipeline(
                batch, settings, merge=len(batch) > 1,
                sequence_name=args.sequence_name or "", progress=progress,
            )
        except (MediaError, ValueError, FileNotFoundError) as exc:
            label = batch[0] if len(batch) == 1 else f"{len(batch)}개 묶음"
            print(f"[오류] {label}: {exc}", file=sys.stderr)
            failures += 1
            continue

        reports.append(result.report())
        if args.json:
            continue

        print()
        for line in result.summary_lines():
            print(line)
        for warning in result.warnings:
            print(f"⚠ {warning}")
        if result.outputs:
            print("\n생성된 파일:")
            for kind, output_path in result.outputs.items():
                print(f"  {kind:<8} {output_path}")
            if "xml" in result.outputs:
                print("\n프리미어에서 파일 > 가져오기 로 위 .xml 을 열면 컷이 적용된 시퀀스가 생성됩니다.")
        elif args.dry_run:
            print(f"\n(미리보기 모드) 예상 절약 시간: {format_duration(result.time_saved)}")

    if args.json:
        print(json.dumps(reports if len(reports) != 1 else reports[0], ensure_ascii=False, indent=2))

    return 1 if failures else 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
