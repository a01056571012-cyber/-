"""ffmpeg로 컷 결과를 실제 파일로 렌더링(미리보기 영상 / 정리된 오디오)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .loudness import LoudnormStats, measure_loudnorm
from .media import find_ffmpeg, run
from .segments import CutPlan


@dataclass
class RenderOptions:
    fade_seconds: float = 0.02
    target_i: float = -16.0
    target_tp: float = -1.5
    target_lra: float = 11.0
    burn_subtitles: Path | None = None
    video_codec: str = "libx264"
    crf: int = 20
    preset: str = "veryfast"
    audio_bitrate: str = "192k"


def build_filter_graph(
    plan: CutPlan,
    *,
    with_video: bool,
    with_audio: bool,
    options: RenderOptions,
    loudnorm: str | None = None,
    subtitles: Path | None = None,
) -> tuple[str, list[str]]:
    """구간 trim → 페이드 → concat → 라우드니스 정규화 필터그래프를 만든다."""
    parts: list[str] = []

    for index, segment in enumerate(plan.segments):
        start = segment.source_in
        end = segment.source_out
        if with_video:
            label = f"v{index}"
            parts.append(
                f"[0:v]trim=start={start:.6f}:end={end:.6f},setpts=PTS-STARTPTS[{label}]"
            )
        if with_audio:
            label = f"a{index}"
            fade = min(options.fade_seconds, max(0.0, segment.duration / 3.0))
            chain = [
                f"[0:a]atrim=start={start:.6f}:end={end:.6f}",
                "asetpts=PTS-STARTPTS",
            ]
            if segment.gain_db:
                chain.append(f"volume={segment.gain_db:.2f}dB")
            if fade > 0.001:
                chain.append(f"afade=t=in:st=0:d={fade:.3f}")
                chain.append(
                    f"afade=t=out:st={max(0.0, segment.duration - fade):.6f}:d={fade:.3f}"
                )
            parts.append(",".join(chain) + f"[{label}]")

    count = len(plan.segments)
    concat_inputs = ""
    for index in range(count):
        if with_video:
            concat_inputs += f"[v{index}]"
        if with_audio:
            concat_inputs += f"[a{index}]"
    parts.append(
        f"{concat_inputs}concat=n={count}:v={1 if with_video else 0}:a={1 if with_audio else 0}"
        f"{'[vcat]' if with_video else ''}{'[acat]' if with_audio else ''}"
    )

    maps: list[str] = []
    if with_video:
        if subtitles:
            escaped = str(subtitles.resolve()).replace("\\", "/").replace(":", r"\:")
            parts.append(f"[vcat]subtitles='{escaped}'[vout]")
            maps.append("[vout]")
        else:
            parts.append("[vcat]null[vout]")
            maps.append("[vout]")
    if with_audio:
        chain = loudnorm or "anull"
        parts.append(f"[acat]{chain}[aout]")
        maps.append("[aout]")

    return ";".join(parts), maps


def _write_graph(graph: str, path: Path) -> Path:
    """필터그래프는 명령줄 길이 제한을 넘길 수 있어 파일로 넘긴다."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(graph, encoding="utf-8")
    return path


def render_preview(
    source: Path,
    plan: CutPlan,
    out_path: Path,
    *,
    options: RenderOptions | None = None,
    with_video: bool = True,
    with_audio: bool = True,
    loudnorm_stats: LoudnormStats | None = None,
    workdir: Path | None = None,
    ffmpeg: str | None = None,
) -> Path:
    options = options or RenderOptions()
    ffmpeg = ffmpeg or find_ffmpeg()
    workdir = workdir or out_path.parent
    if not plan.segments:
        raise ValueError("렌더링할 컷이 없습니다.")

    loudnorm_filter = None
    if with_audio:
        if loudnorm_stats:
            loudnorm_filter = loudnorm_stats.to_filter(
                target_i=options.target_i, target_tp=options.target_tp, target_lra=options.target_lra
            )
        else:
            loudnorm_filter = (
                f"loudnorm=I={options.target_i}:TP={options.target_tp}:LRA={options.target_lra}"
            )

    graph, maps = build_filter_graph(
        plan,
        with_video=with_video,
        with_audio=with_audio,
        options=options,
        loudnorm=loudnorm_filter,
        subtitles=options.burn_subtitles,
    )
    script = _write_graph(graph, workdir / "filtergraph.txt")

    cmd = [
        ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
        "-i", str(source),
        "-filter_complex_script", str(script),
    ]
    for label in maps:
        cmd += ["-map", label]
    if with_video:
        cmd += ["-c:v", options.video_codec, "-crf", str(options.crf), "-preset", options.preset,
                "-pix_fmt", "yuv420p"]
    if with_audio:
        if out_path.suffix.lower() == ".wav":
            cmd += ["-c:a", "pcm_s16le"]
        else:
            cmd += ["-c:a", "aac", "-b:a", options.audio_bitrate]
    cmd.append(str(out_path))

    out_path.parent.mkdir(parents=True, exist_ok=True)
    run(cmd)
    return out_path


def render_audio(
    source: Path,
    plan: CutPlan,
    out_path: Path,
    *,
    options: RenderOptions | None = None,
    workdir: Path | None = None,
    ffmpeg: str | None = None,
) -> Path:
    """컷 타임라인에 맞춘 정규화 오디오(WAV)만 렌더링한다."""
    return render_preview(
        source, plan, out_path, options=options, with_video=False, with_audio=True,
        workdir=workdir, ffmpeg=ffmpeg,
    )


def measure_for_render(source: Path, options: RenderOptions, *, ffmpeg: str | None = None) -> LoudnormStats | None:
    return measure_loudnorm(
        source,
        target_i=options.target_i,
        target_tp=options.target_tp,
        target_lra=options.target_lra,
        ffmpeg=ffmpeg,
    )
