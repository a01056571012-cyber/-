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
from .audio import Envelope
from .silence import Threshold, detect_speech_regions, refine_region_edges
from .config import Settings
from .exporters.edl import write_edl
from .exporters.fcpxml import write_fcpxml
from .exporters.jsx import write_jsx
from .loudness import LoudnessTrack, balance_plans, measure_track
from .media import MediaError, MediaInfo, extract_analysis_wav, find_ffmpeg, probe
from .render import measure_for_render, render_audio, render_preview
from .segments import CutPlan, ShapeOptions, shape_regions
from .script import ScriptEntry, group_by_source, read_script, write_script
from .subtitles import Cue, build_cues, regions_from_transcript, write_srt, write_vtt
from .timecode import format_duration
from .timeline import Timeline, build_timeline
from .transcribe import Transcript, TranscriptionUnavailable, transcribe

Progress = Callable[[str, str], None]


@dataclass
class SourceAnalysis:
    """원본 하나에 대한 분석 결과."""

    info: MediaInfo
    plan: CutPlan | None
    track: LoudnessTrack
    wav: Path
    envelope: Envelope
    threshold: Threshold
    regions: list[tuple[float, float]] = field(default_factory=list)
    protected: list[tuple[float, float]] = field(default_factory=list)
    transcript: Transcript | None = None


@dataclass
class Result:
    timeline: Timeline
    cues: list[Cue] = field(default_factory=list)
    outputs: dict[str, Path] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    program_lufs: float | None = None
    transcript_backend: str = ""
    script_entries: list[ScriptEntry] = field(default_factory=list)

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

    track = LoudnessTrack([])
    if settings.balance.enabled:
        progress("balance", f"[{source.name}] 구간별 라우드니스를 측정하는 중")
        try:
            track = measure_track(source, ffmpeg=ffmpeg)
        except RuntimeError as exc:
            warnings.append(f"{source.name}: 라우드니스 측정 실패 ({exc})")

    return SourceAnalysis(info=info, plan=None, track=track, wav=wav,
                          envelope=envelope, threshold=threshold, regions=regions)


def _shape_with(analysis: SourceAnalysis, offset_db: float, shape: ShapeOptions) -> CutPlan:
    """임계값을 offset_db 만큼 올려/내려 다시 컷을 잡는다."""
    threshold = replace(
        analysis.threshold,
        open_db=analysis.threshold.open_db + offset_db,
        close_db=analysis.threshold.close_db + offset_db,
    )
    regions = detect_speech_regions(analysis.envelope, threshold)
    regions = refine_region_edges(analysis.envelope, regions, threshold)
    if analysis.protected:
        regions = sorted(regions + analysis.protected)
    return shape_regions(
        regions, duration=analysis.info.duration, options=shape,
        frame_rate=analysis.info.frame_rate,
    )


def fit_to_target(analyses: list[SourceAnalysis], shape: ShapeOptions, target: float,
                  *, tolerance: float = 0.03) -> tuple[list[CutPlan], float, ShapeOptions]:
    """목표 길이에 가장 가깝도록 무음 임계값(필요하면 여유까지)을 자동으로 조절한다.

    임계값을 올릴수록 더 많은 소리가 무음으로 판정되어 결과가 짧아진다. 임계값만으로
    부족하면 앞뒤 여유와 최소 무음 길이까지 단계적으로 줄인다. 엔벨로프는 이미
    계산되어 있으므로 다시 훑기만 하면 되고, 오디오를 다시 읽지는 않는다.
    """
    attempts = [
        shape,
        replace(shape, lead_in=shape.lead_in * 0.5, lead_out=shape.lead_out * 0.5,
                min_silence=max(0.15, shape.min_silence * 0.6)),
        # 마지막 단계에서는 최소 컷 길이를 오히려 늘린다. 짧은 조각이 사라지면서
        # 길이는 더 줄고, 결과도 잘게 튀지 않아 보기 편해진다.
        replace(shape, lead_in=shape.lead_in * 0.2, lead_out=shape.lead_out * 0.2,
                min_silence=0.12, min_clip=max(shape.min_clip, 0.6)),
    ]

    best: tuple[list[CutPlan], float, ShapeOptions] | None = None
    best_gap = float("inf")

    for options in attempts:
        low, high = -15.0, 30.0
        for _ in range(18):
            middle = (low + high) / 2
            plans = [_shape_with(analysis, middle, options) for analysis in analyses]
            total = sum(plan.kept_duration for plan in plans)
            gap = abs(total - target)
            if gap < best_gap:
                best, best_gap = (plans, middle, options), gap
            if total > target:
                low = middle
            else:
                high = middle
        if best is not None and best_gap <= target * tolerance:
            break

    assert best is not None
    return best


def _build_plans(analyses: list[SourceAnalysis], shape: ShapeOptions,
                 warnings: list[str]) -> None:
    for analysis in analyses:
        plan = shape_regions(
            analysis.regions, duration=analysis.info.duration, options=shape,
            frame_rate=analysis.info.frame_rate,
        )
        if not plan.segments:
            warnings.append(
                f"{analysis.info.path.name}: 남길 구간을 찾지 못해 전체를 한 컷으로 유지합니다."
            )
            plan = shape_regions(
                [(0.0, analysis.info.duration)], duration=analysis.info.duration,
                options=shape, frame_rate=analysis.info.frame_rate,
            )
        analysis.plan = plan


def _protect_edges(analyses: list[SourceAnalysis], shape: ShapeOptions) -> None:
    """맨 앞 인사말과 맨 끝 마무리가 잘려나가지 않도록 통째로 지킨다."""
    if not analyses:
        return
    if shape.keep_head > 0:
        first = analyses[0]
        first.protected.append((0.0, min(shape.keep_head, first.info.duration)))
    if shape.keep_tail > 0:
        last = analyses[-1]
        last.protected.append((max(0.0, last.info.duration - shape.keep_tail), last.info.duration))
    for analysis in analyses:
        if analysis.protected:
            analysis.regions = sorted(analysis.regions + analysis.protected)


def _regions_from_sentences(analyses: list[SourceAnalysis], settings: Settings,
                            progress: Progress, warnings: list[str],
                            supplied: Transcript | None) -> None:
    """음성을 인식해 문장 단위로 남길 구간을 잡는다."""
    for index, analysis in enumerate(analyses):
        name = analysis.info.path.name
        progress("subtitle", f"[{name}] 문장을 찾기 위해 음성을 인식하는 중")
        try:
            analysis.transcript = supplied if supplied is not None else transcribe(
                analysis.wav, settings.transcribe,
                progress=_transcribe_reporter(name, progress),
            )
        except Exception as exc:
            warnings.append(
                f"{name}: 음성 인식에 실패해 무음 기준으로 자릅니다 ({exc})"
            )
            continue
        regions = regions_from_transcript(
            analysis.transcript, duration=analysis.info.duration,
            lead_in=settings.shape.lead_in, lead_out=settings.shape.lead_out,
        )
        if regions:
            analysis.regions = regions
        else:
            warnings.append(f"{name}: 인식된 말이 없어 무음 기준으로 자릅니다.")


def _regions_from_script(analyses: list[SourceAnalysis], entries: list[ScriptEntry],
                         warnings: list[str]) -> None:
    """대본에 남아 있는 줄만 그대로 사용한다."""
    grouped = group_by_source(entries)
    unknown = set(grouped) - {analysis.info.path.name for analysis in analyses}
    if unknown:
        warnings.append(f"대본에 있지만 처리 대상에 없는 파일: {', '.join(sorted(unknown))}")
    for analysis in analyses:
        spans = grouped.get(analysis.info.path.name, [])
        analysis.regions = [
            (max(0.0, start), min(analysis.info.duration, end))
            for start, end in spans
            if end > start
        ]
        if not spans:
            warnings.append(f"{analysis.info.path.name}: 대본에 남은 구간이 없습니다.")


def _fit(analyses: list[SourceAnalysis], settings: Settings, shape: ShapeOptions,
         progress: Progress, warnings: list[str]) -> None:
    target = settings.target_duration
    progress("fit", f"목표 길이 {format_duration(target)}에 맞추는 중")

    if settings.cut_by == "sentence" or settings.script_path:
        # 문장 단위로 잡았을 때는 임계값을 건드리지 않고 문장을 통째로 덜어낸다.
        removed = _drop_shortest(analyses, shape, target)
        _build_plans(analyses, shape, warnings)
        total = sum(a.plan.kept_duration for a in analyses if a.plan)
        progress("fit", f"짧은 문장 {removed}개를 덜어내 {format_duration(total)}")
    else:
        plans, offset, used_shape = fit_to_target(analyses, shape, target)
        for analysis, plan in zip(analyses, plans):
            analysis.plan = plan
        total = sum(plan.kept_duration for plan in plans)
        progress(
            "fit",
            f"임계값 {offset:+.1f}dB 조정 → {format_duration(total)} "
            f"(여유 {used_shape.lead_in:.2f}/{used_shape.lead_out:.2f}초)",
        )

    if total > target * 1.05:
        warnings.append(
            f"{format_duration(target)}까지 줄일 수 없어 {format_duration(total)}가 한계입니다. "
            "남길 내용이 그만큼 많습니다."
        )
    elif total < target * 0.8:
        warnings.append(
            f"목표 {format_duration(target)}보다 짧은 {format_duration(total)}가 됐습니다."
        )


def _drop_shortest(analyses: list[SourceAnalysis], shape: ShapeOptions, target: float) -> int:
    """짧은 구간부터 통째로 덜어내 목표 길이에 맞춘다.

    짧은 말일수록 추임새나 말 끊김일 가능성이 높다. 문장 중간을 자르지 않으므로
    남은 부분은 맥락이 유지된다. 앞뒤로 지키기로 한 구간은 건드리지 않는다.
    """
    head_limit = shape.keep_head
    protected: list[tuple[int, int]] = []
    candidates: list[tuple[float, int, int]] = []

    for source_index, analysis in enumerate(analyses):
        tail_start = analysis.info.duration - shape.keep_tail
        for region_index, (start, end) in enumerate(analysis.regions):
            is_head = source_index == 0 and start < head_limit
            is_tail = source_index == len(analyses) - 1 and shape.keep_tail > 0 and end > tail_start
            if is_head or is_tail:
                protected.append((source_index, region_index))
            else:
                candidates.append((end - start, source_index, region_index))

    total = sum(end - start for a in analyses for start, end in a.regions)
    candidates.sort()
    dropped: set[tuple[int, int]] = set()
    for length, source_index, region_index in candidates:
        if total <= target:
            break
        dropped.add((source_index, region_index))
        total -= length

    for source_index, analysis in enumerate(analyses):
        analysis.regions = [
            span for region_index, span in enumerate(analysis.regions)
            if (source_index, region_index) not in dropped
        ]
    return len(dropped)


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

        script_entries = None
        if settings.script_path:
            progress("script", f"대본을 읽는 중: {Path(settings.script_path).name}")
            script_entries = read_script(Path(settings.script_path))

        shape = settings.shape
        if script_entries is not None:
            _regions_from_script(analyses, script_entries, warnings)
            # 대본의 구간은 사람이 정한 값이므로 여유를 덧붙이지 않고 그대로 쓴다.
            shape = replace(shape, lead_in=0.0, lead_out=0.0, min_silence=0.0,
                            min_clip=0.0, max_silence_keep=0.0)
        elif settings.cut_by == "sentence":
            _regions_from_sentences(analyses, settings, progress, warnings, transcript)

        _protect_edges(analyses, shape)
        _build_plans(analyses, shape, warnings)

        if settings.target_duration:
            _fit(analyses, settings, shape, progress, warnings)

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

        result.script_entries = _script_entries(timeline, analyses)

        progress("export", "편집 파일을 쓰는 중")
        _export(result, settings, outdir, workdir, ffmpeg, default_name)
        return result
    finally:
        if not settings.output.keep_workdir:
            shutil.rmtree(workdir, ignore_errors=True)


def _transcribe_reporter(name: str, progress: Progress, step: int = 5):
    """음성 인식 진행률을 일정 간격으로만 알린다.

    이 단계는 몇 분에서 몇십 분까지 걸리는데 아무 표시가 없으면 멈춘 것처럼
    보이기 때문에, 인식된 지점을 기준으로 진행률을 보여준다.
    """
    state = {"next": step}

    def report(done: float, total: float) -> None:
        if total <= 0:
            return
        percent = min(100, int(done / total * 100))
        if percent < state["next"]:
            return
        state["next"] = percent - percent % step + step
        progress(
            "subtitle",
            f"[{name}] 음성 인식 {percent}% "
            f"({format_duration(done)} / {format_duration(total)})",
        )

    return report


def _make_subtitles(analyses: list[SourceAnalysis], timeline: Timeline, settings: Settings,
                    progress: Progress, warnings: list[str],
                    supplied: Transcript | None) -> tuple[list[Cue], str]:
    """원본마다 음성을 인식해 최종 시퀀스 시간축의 자막으로 합친다."""
    cues: list[Cue] = []
    backend = ""
    for index, analysis in enumerate(analyses):
        name = analysis.info.path.name
        if supplied is None and analysis.transcript is None:
            progress(
                "subtitle",
                f"[{name}] 인식 모델({settings.transcribe.model})을 준비하는 중 "
                "— 처음 한 번은 내려받느라 몇 분 걸립니다",
            )
        try:
            transcript = supplied or analysis.transcript
            if transcript is None:
                transcript = transcribe(
                    analysis.wav, settings.transcribe,
                    progress=_transcribe_reporter(name, progress),
                )
            backend = transcript.backend
            progress("subtitle", f"[{name}] 음성 인식 완료")
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


def _script_entries(timeline: Timeline, analyses: list[SourceAnalysis]) -> list[ScriptEntry]:
    """편집 결과를 사람이 고칠 수 있는 대본 항목으로 옮긴다."""
    entries: list[ScriptEntry] = []
    for index, clip in enumerate(timeline.place(), start=1):
        analysis = analyses[clip.item_index]
        text = ""
        if analysis.transcript is not None:
            spoken = [
                utterance.text.strip()
                for utterance in analysis.transcript.utterances
                if utterance.end > clip.segment.source_in
                and utterance.start < clip.segment.source_out
            ]
            text = " ".join(spoken)
            if len(text) > 120:
                text = text[:117] + "..."
        entries.append(
            ScriptEntry(
                index=index,
                source=clip.item.info.path.name,
                start=clip.segment.source_in,
                end=clip.segment.source_out,
                text=text,
            )
        )
    return entries


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

    if out.write_script and result.script_entries:
        result.outputs["script"] = write_script(
            result.script_entries, outdir / f"{prefix}_대본.txt"
        )

    if out.write_report:
        report_path = outdir / f"{prefix}_report.json"
        report_path.write_text(
            json.dumps(result.report(), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        result.outputs["report"] = report_path
