"""전체 파이프라인: 분석 → 컷 → 밸런스 → 자막 → 내보내기."""

from __future__ import annotations

import json
import shutil
import tempfile
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Callable

from . import audio as audio_mod
from . import silence as silence_mod
from .config import Settings
from .exporters.edl import write_edl
from .exporters.fcpxml import write_fcpxml
from .exporters.jsx import write_jsx
from .loudness import LoudnessTrack, apply_segment_balance, measure_track
from .media import MediaError, MediaInfo, extract_analysis_wav, find_ffmpeg, probe
from .render import measure_for_render, render_audio, render_preview
from .segments import CutPlan, shape_regions
from .subtitles import Cue, build_cues, write_srt, write_vtt
from .timecode import format_duration
from .transcribe import Transcript, TranscriptionUnavailable, transcribe

Progress = Callable[[str, str], None]


@dataclass
class Result:
    source: Path
    info: MediaInfo
    plan: CutPlan
    cues: list[Cue] = field(default_factory=list)
    outputs: dict[str, Path] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    program_lufs: float | None = None
    transcript_backend: str = ""

    @property
    def time_saved(self) -> float:
        return self.plan.removed_duration

    def report(self) -> dict:
        segments = [
            {
                "index": index + 1,
                "source_in": round(seg.source_in, 3),
                "source_out": round(seg.source_out, 3),
                "timeline_start": round(seg.timeline_start, 3),
                "duration": round(seg.duration, 3),
                "gain_db": seg.gain_db,
                "loudness_lufs": round(seg.loudness_lufs, 2) if seg.loudness_lufs is not None else None,
            }
            for index, seg in enumerate(self.plan.segments)
        ]
        return {
            "source": str(self.source),
            "duration": round(self.info.duration, 3),
            "fps": round(self.info.fps, 4),
            "resolution": [self.info.width, self.info.height],
            "cuts": len(self.plan.segments),
            "kept_duration": round(self.plan.kept_duration, 3),
            "removed_duration": round(self.plan.removed_duration, 3),
            "removed_ratio": round(
                self.plan.removed_duration / self.info.duration if self.info.duration else 0.0, 4
            ),
            "program_lufs": round(self.program_lufs, 2) if self.program_lufs is not None else None,
            "subtitle_cues": len(self.cues),
            "transcript_backend": self.transcript_backend,
            "warnings": self.warnings,
            "outputs": {k: str(v) for k, v in self.outputs.items()},
            "segments": segments,
        }

    def summary_lines(self) -> list[str]:
        lines = [
            f"원본        : {self.source.name} ({format_duration(self.info.duration)}, "
            f"{self.info.fps:.2f}fps)",
            f"컷          : {len(self.plan.segments)}개 / 남긴 길이 "
            f"{format_duration(self.plan.kept_duration)}",
            f"잘라낸 무음 : {format_duration(self.plan.removed_duration)} "
            f"({self.plan.removed_duration / self.info.duration * 100:.1f}%)"
            if self.info.duration
            else "잘라낸 무음 : -",
        ]
        if self.program_lufs is not None:
            gains = [seg.gain_db for seg in self.plan.segments]
            lines.append(
                f"라우드니스  : 평균 {self.program_lufs:.1f} LUFS / 보정 "
                f"{min(gains):+.1f}~{max(gains):+.1f} dB"
                if gains
                else f"라우드니스  : 평균 {self.program_lufs:.1f} LUFS"
            )
        if self.cues:
            lines.append(f"자막        : {len(self.cues)}개 ({self.transcript_backend})")
        return lines


def _noop(stage: str, message: str) -> None:  # pragma: no cover - 기본 콜백
    pass


def run_pipeline(
    source: Path,
    settings: Settings,
    *,
    progress: Progress | None = None,
    transcript: Transcript | None = None,
) -> Result:
    progress = progress or _noop
    source = Path(source).expanduser().resolve()
    ffmpeg = find_ffmpeg()

    progress("probe", "미디어 정보를 읽는 중")
    info = probe(source)
    if not info.has_audio:
        raise MediaError("오디오 트랙이 없어 무음 분석을 할 수 없습니다.")
    if info.duration <= 0:
        raise MediaError("길이를 알 수 없는 미디어입니다.")

    outdir = settings.output.outdir or source.parent / f"{source.stem}_precut"
    outdir = Path(outdir).expanduser().resolve()
    outdir.mkdir(parents=True, exist_ok=True)
    workdir = Path(tempfile.mkdtemp(prefix="precut-", dir=str(outdir)))
    warnings: list[str] = []

    try:
        progress("extract", "분석용 오디오를 추출하는 중")
        wav = extract_analysis_wav(
            source, workdir / "analysis.wav", sample_rate=settings.detect.analysis_rate, ffmpeg=ffmpeg
        )

        progress("analyze", "무음 구간을 찾는 중")
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
            warnings.append("발화 구간을 찾지 못해 원본 전체를 한 컷으로 유지합니다.")
            plan = shape_regions(
                [(0.0, info.duration)],
                duration=info.duration,
                options=settings.shape,
                frame_rate=info.frame_rate,
            )

        program_lufs = None
        if settings.balance.enabled:
            progress("balance", "구간별 라우드니스를 측정하는 중")
            try:
                track = measure_track(source, ffmpeg=ffmpeg)
            except RuntimeError as exc:
                warnings.append(f"라우드니스 측정 실패: {exc}")
                track = LoudnessTrack([])
            _, program_lufs = apply_segment_balance(plan, track, settings.balance)

        cues: list[Cue] = []
        backend = ""
        if settings.make_subtitles:
            progress("subtitle", "음성을 인식하는 중 (시간이 걸릴 수 있습니다)")
            try:
                if transcript is None:
                    transcript = transcribe(wav, settings.transcribe)
                backend = transcript.backend
                cues = build_cues(
                    transcript,
                    plan.time_map(),
                    settings.subtitles,
                    timeline_duration=plan.kept_duration,
                )
            except TranscriptionUnavailable as exc:
                warnings.append(str(exc))
            except Exception as exc:  # STT 실패가 컷 결과까지 버리게 두지 않는다
                warnings.append(f"자막 생성 실패: {exc}")

        result = Result(
            source=source,
            info=info,
            plan=plan,
            cues=cues,
            warnings=warnings,
            program_lufs=program_lufs,
            transcript_backend=backend,
        )

        progress("export", "편집 파일을 쓰는 중")
        _export(result, settings, outdir, workdir, ffmpeg)
        return result
    finally:
        if not settings.output.keep_workdir:
            shutil.rmtree(workdir, ignore_errors=True)


def _export(result: Result, settings: Settings, outdir: Path, workdir: Path, ffmpeg: str) -> None:
    prefix = settings.output.prefix or result.source.stem
    out = settings.output
    sequence_name = settings.xml.sequence_name
    if sequence_name == "precut sequence":
        sequence_name = f"{prefix} (precut)"
        settings.xml.sequence_name = sequence_name

    srt_path: Path | None = None
    if out.write_srt and result.cues:
        srt_path = write_srt(result.cues, outdir / f"{prefix}.srt")
        result.outputs["srt"] = srt_path
    if out.write_vtt and result.cues:
        result.outputs["vtt"] = write_vtt(result.cues, outdir / f"{prefix}.vtt")

    if out.write_xml:
        result.outputs["xml"] = write_fcpxml(
            result.plan, result.info, outdir / f"{prefix}.xml", settings.xml
        )
    if out.write_edl:
        result.outputs["edl"] = write_edl(
            result.plan, result.info, outdir / f"{prefix}.edl", title=sequence_name
        )

    audio_path: Path | None = None
    if out.render_audio:
        audio_path = render_audio(
            result.source,
            result.plan,
            outdir / f"{prefix}_balanced.wav",
            options=settings.render,
            workdir=workdir,
            ffmpeg=ffmpeg,
        )
        result.outputs["audio"] = audio_path

    if out.render_preview:
        render_options = replace(settings.render)
        if out.burn_subtitles and srt_path:
            render_options.burn_subtitles = srt_path
        stats = measure_for_render(result.source, render_options, ffmpeg=ffmpeg)
        result.outputs["preview"] = render_preview(
            result.source,
            result.plan,
            outdir / f"{prefix}_preview.mp4",
            options=render_options,
            loudnorm_stats=stats,
            workdir=workdir,
            ffmpeg=ffmpeg,
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
