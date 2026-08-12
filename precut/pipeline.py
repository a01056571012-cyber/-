"""전체 파이프라인: 분석 → 컷 → 밸런스 → 자막 → 내보내기."""

from __future__ import annotations

import json
import shutil
import tempfile
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Callable, Sequence

from . import audio as audio_mod
from . import silence as silence_mod
from .config import Settings
from .exporters.edl import write_edl
from .exporters.fcpxml import write_fcpxml
from .exporters.jsx import write_jsx
from .loudness import LoudnessTrack, balance_plans, measure_track
from .media import MediaError, MediaInfo, extract_analysis_wav, find_ffmpeg, probe
from .render import measure_for_render, render_audio, render_preview
from .segments import CutPlan, shape_regions
from .subtitles import Cue, build_cues, write_srt, write_vtt
from .timecode import format_duration
from .timeline import Timeline, build_timeline
from .transcribe import Transcript, TranscriptionUnavailable, transcribe

Progress = Callable[[str, str], None]


@dataclass
class SourceAnalysis:
    """원본 하나에 대한 분석 결과."""

    info: MediaInfo
    plan: CutPlan
    track: LoudnessTrack
    wav: Path


@dataclass
class Result:
    timeline: Timeline
    cues: list[Cue] = field(default_factory=list)
    outputs: dict[str, Path] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    program_lufs: float | None = None
    transcript_backend: str = ""

    @property
    def sources(self) -> list[Path]:
        return [item.info.path for item in self.timeline.items]

    @property
    def source(self) -> Path:
        return self.timeline.items[0].info.path

    @property
    def info(self) -> MediaInfo:
        return self.timeline.items[0].info

    @property
    def plan(self) -> CutPlan:
        return self.timeline.items[0].plan

    @property
    def segments(self):
        return [clip.segment for clip in self.timeline.place()]

    @property
    def time_saved(self) -> float:
        return self.timeline.removed_duration

    def report(self) -> dict:
        segments = []
        for index, clip in enumerate(self.timeline.place(), start=1):
            segment = clip.segment
            segments.append(
                {
                    "index": index,
                    "source": clip.item.info.path.name,
                    "source_in": round(segment.source_in, 3),
                    "source_out": round(segment.source_out, 3),
                    "timeline_start": round(
                        self.timeline.frame_rate.to_seconds(clip.start_frame), 3
                    ),
                    "duration": round(segment.duration, 3),
                    "gain_db": segment.gain_db,
                    "loudness_lufs": round(segment.loudness_lufs, 2)
                    if segment.loudness_lufs is not None
                    else None,
                }
            )

        timeline = self.timeline
        return {
            "sources": [str(path) for path in self.sources],
            "merged": timeline.is_merged,
            "duration": round(timeline.source_duration, 3),
            "fps": round(timeline.frame_rate.fps, 4),
            "cuts": len(segments),
            "kept_duration": round(timeline.duration, 3),
            "removed_duration": round(timeline.removed_duration, 3),
            "removed_ratio": round(
                timeline.removed_duration / timeline.source_duration
                if timeline.source_duration
                else 0.0,
                4,
            ),
            "program_lufs": round(self.program_lufs, 2) if self.program_lufs is not None else None,
            "subtitle_cues": len(self.cues),
            "transcript_backend": self.transcript_backend,
            "warnings": self.warnings,
            "outputs": {k: str(v) for k, v in self.outputs.items()},
            "segments": segments,
        }

    def summary_lines(self) -> list[str]:
        timeline = self.timeline
        source_duration = timeline.source_duration
        if timeline.is_merged:
            first = f"원본        : 영상 {len(timeline.items)}개를 하나로 이어붙임 " \
                    f"({format_duration(source_duration)})"
        else:
            first = (
                f"원본        : {self.source.name} ({format_duration(source_duration)}, "
                f"{timeline.frame_rate.fps:.2f}fps)"
            )
        lines = [
            first,
            f"컷          : {len(timeline.place())}개 / 남긴 길이 "
            f"{format_duration(timeline.duration)}",
        ]
        if source_duration:
            lines.append(
                f"잘라낸 무음 : {format_duration(timeline.removed_duration)} "
                f"({timeline.removed_duration / source_duration * 100:.1f}%)"
            )
        if self.program_lufs is not None:
            gains = [clip.segment.gain_db for clip in timeline.place()]
            suffix = f" / 보정 {min(gains):+.1f}~{max(gains):+.1f} dB" if gains else ""
            lines.append(f"라우드니스  : 평균 {self.program_lufs:.1f} LUFS{suffix}")
        if self.cues:
            lines.append(f"자막        : {len(self.cues)}개 ({self.transcript_backend})")
        return lines


def _noop(stage: str, message: str) -> None:  # pragma: no cover - 기본 콜백
    pass


def _analyze(source: Path, settings: Settings, workdir: Path, ffmpeg: str, index: int,
             progress: Progress, warnings: list[str]) -> SourceAnalysis:
    """원본 하나를 분석해 컷 계획과 라우드니스 측정을 만든다."""
    info = probe(source)
    if not info.has_audio:
        raise MediaError(f"오디오 트랙이 없어 무음 분석을 할 수 없습니다: {source.name}")
    if info.duration <= 0:
        raise MediaError(f"길이를 알 수 없는 미디어입니다: {source.name}")

    progress("extract", f"[{source.name}] 분석용 오디오를 추출하는 중")
    wav = extract_analysis_wav(
        source, workdir / f"analysis-{index}.wav", sample_rate=settings.detect.analysis_rate,
        ffmpeg=ffmpeg,
    )

    progress("analyze", f"[{source.name}] 무음 구간을 찾는 중")
    samples, rate = audio_mod.read_wav_mono(wav)
    envelope = audio_mod.compute_envelope(
        samples, rate, hop_ms=settings.detect.hop_ms, window_ms=settings.detect.window_ms
    )
    threshold = silence_mod.choose_threshold(
        envelope,
        threshold_db=settings.detect.threshold_db,
        hysteresis_db=settings.detect.hysteresis_db,
        noise_margin_db=settings.detect.noise_margin_db,
    )
    progress("analyze", threshold.describe())
    regions = silence_mod.detect_speech_regions(envelope, threshold)
    regions = silence_mod.refine_region_edges(envelope, regions, threshold)

    plan = shape_regions(
        regions, duration=info.duration, options=settings.shape, frame_rate=info.frame_rate
    )
    if not plan.segments:
        warnings.append(f"{source.name}: 발화 구간을 찾지 못해 전체를 한 컷으로 유지합니다.")
        plan = shape_regions(
            [(0.0, info.duration)], duration=info.duration, options=settings.shape,
            frame_rate=info.frame_rate,
        )

    track = LoudnessTrack([])
    if settings.balance.enabled:
        progress("balance", f"[{source.name}] 구간별 라우드니스를 측정하는 중")
        try:
            track = measure_track(source, ffmpeg=ffmpeg)
        except RuntimeError as exc:
            warnings.append(f"{source.name}: 라우드니스 측정 실패 ({exc})")

    return SourceAnalysis(info=info, plan=plan, track=track, wav=wav)


def run_pipeline(
    sources: Path | Sequence[Path],
    settings: Settings,
    *,
    merge: bool = False,
    sequence_name: str = "",
    progress: Progress | None = None,
    transcript: Transcript | None = None,
) -> Result:
    """원본 하나 또는 여럿을 처리한다.

    merge=True이면 원본들을 순서대로 이어 붙여 하나의 시퀀스로 만든다.
    """
    progress = progress or _noop
    if isinstance(sources, (str, Path)):
        sources = [Path(sources)]
    paths = [Path(item).expanduser().resolve() for item in sources]
    if not paths:
        raise ValueError("처리할 파일이 없습니다.")
    ffmpeg = find_ffmpeg()

    base = paths[0]
    default_name = base.stem if len(paths) == 1 else f"{base.stem} 외 {len(paths) - 1}개"
    outdir = settings.output.outdir or base.parent / f"{default_name}_precut"
    outdir = Path(outdir).expanduser().resolve()
    outdir.mkdir(parents=True, exist_ok=True)
    workdir = Path(tempfile.mkdtemp(prefix="precut-", dir=str(outdir)))
    warnings: list[str] = []

    try:
        progress("probe", "미디어 정보를 읽는 중")
        analyses = [
            _analyze(path, settings, workdir, ffmpeg, index, progress, warnings)
            for index, path in enumerate(paths)
        ]

        _, program_lufs = balance_plans(
            [(item.plan, item.track) for item in analyses], settings.balance
        )

        timeline = build_timeline(
            [(item.info, item.plan) for item in analyses],
            name=sequence_name or f"{default_name} (precut)",
        )

        cues: list[Cue] = []
        backend = ""
        if settings.make_subtitles:
            cues, backend = _make_subtitles(
                analyses, timeline, settings, progress, warnings, transcript
            )

        result = Result(
            timeline=timeline,
            cues=cues,
            warnings=warnings,
            program_lufs=program_lufs,
            transcript_backend=backend,
        )

        progress("export", "편집 파일을 쓰는 중")
        _export(result, settings, outdir, workdir, ffmpeg, default_name)
        return result
    finally:
        if not settings.output.keep_workdir:
            shutil.rmtree(workdir, ignore_errors=True)


def _make_subtitles(analyses: list[SourceAnalysis], timeline: Timeline, settings: Settings,
                    progress: Progress, warnings: list[str],
                    supplied: Transcript | None) -> tuple[list[Cue], str]:
    """원본마다 음성을 인식해 최종 시퀀스 시간축의 자막으로 합친다."""
    cues: list[Cue] = []
    backend = ""
    for index, analysis in enumerate(analyses):
        progress("subtitle", f"[{analysis.info.path.name}] 음성을 인식하는 중 (시간이 걸립니다)")
        try:
            transcript = supplied if supplied is not None else transcribe(
                analysis.wav, settings.transcribe
            )
            backend = transcript.backend
            cues.extend(
                build_cues(
                    transcript,
                    timeline.time_map(index),
                    settings.subtitles,
                    timeline_duration=timeline.duration,
                )
            )
        except TranscriptionUnavailable as exc:
            warnings.append(str(exc))
            return [], ""
        except Exception as exc:  # STT 실패가 컷 결과까지 버리게 두지 않는다
            warnings.append(f"{analysis.info.path.name}: 자막 생성 실패 ({exc})")

    cues.sort(key=lambda cue: cue.start)
    for position, cue in enumerate(cues, start=1):
        cue.index = position
    return cues, backend


def _export(result: Result, settings: Settings, outdir: Path, workdir: Path, ffmpeg: str,
            default_name: str) -> None:
    prefix = settings.output.prefix or default_name
    out = settings.output
    sequence_name = settings.xml.sequence_name
    if sequence_name == "precut sequence":
        sequence_name = result.timeline.name
        settings.xml.sequence_name = sequence_name

    srt_path: Path | None = None
    if out.write_srt and result.cues:
        srt_path = write_srt(result.cues, outdir / f"{prefix}.srt")
        result.outputs["srt"] = srt_path
    if out.write_vtt and result.cues:
        result.outputs["vtt"] = write_vtt(result.cues, outdir / f"{prefix}.vtt")

    if out.write_xml:
        result.outputs["xml"] = write_fcpxml(
            result.timeline, outdir / f"{prefix}.xml", settings.xml
        )
    if out.write_edl:
        result.outputs["edl"] = write_edl(
            result.timeline, outdir / f"{prefix}.edl", title=sequence_name
        )

    render_requested = out.render_audio or out.render_preview
    if render_requested and result.timeline.is_merged:
        # 렌더링은 원본 하나를 통째로 다시 인코딩하는 방식이라 이어붙이기와 함께 쓸 수 없다.
        result.warnings.append(
            "여러 영상을 이어붙일 때는 미리보기·오디오 렌더링을 지원하지 않습니다. "
            "XML을 프리미어에서 열어 확인하세요."
        )
        out = replace(out, render_audio=False, render_preview=False)

    audio_path: Path | None = None
    if out.render_audio:
        audio_path = render_audio(
            result.source, result.plan, outdir / f"{prefix}_balanced.wav",
            options=settings.render, workdir=workdir, ffmpeg=ffmpeg,
        )
        result.outputs["audio"] = audio_path

    if out.render_preview:
        render_options = replace(settings.render)
        if out.burn_subtitles and srt_path:
            render_options.burn_subtitles = srt_path
        stats = measure_for_render(result.source, render_options, ffmpeg=ffmpeg)
        result.outputs["preview"] = render_preview(
            result.source, result.plan, outdir / f"{prefix}_preview.mp4",
            options=render_options, loudnorm_stats=stats, workdir=workdir, ffmpeg=ffmpeg,
        )

    if out.write_jsx and "xml" in result.outputs:
        result.outputs["jsx"] = write_jsx(
            outdir / f"{prefix}_import.jsx",
            xml_path=result.outputs["xml"],
            sequence_name=sequence_name,
            srt_path=srt_path,
            audio_path=audio_path,
        )

    if out.write_report:
        report_path = outdir / f"{prefix}_report.json"
        report_path.write_text(
            json.dumps(result.report(), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        result.outputs["report"] = report_path
