"""CMX3600 EDL 출력 (다빈치 리졸브·구형 NLE용 대체 경로)."""

from __future__ import annotations

from pathlib import Path

from ..timeline import Timeline


def _reel_name(stem: str, index: int) -> str:
    cleaned = "".join(ch for ch in stem.upper() if ch.isalnum())[:8]
    return cleaned or f"AX{index:02d}"


def render_edl(timeline: Timeline, *, title: str = "precut") -> str:
    rate = timeline.frame_rate
    lines = [f"TITLE: {title}", "FCM: " + ("DROP FRAME" if rate.ntsc else "NON-DROP FRAME"), ""]

    for index, clip in enumerate(timeline.place(), start=1):
        info = clip.item.info
        reel = _reel_name(info.path.stem, clip.item_index + 1)
        record_in = rate.to_seconds(clip.start_frame)
        record_out = rate.to_seconds(clip.end_frame)
        for channel in ("V", "A"):
            if channel == "V" and not info.has_video:
                continue
            if channel == "A" and not info.has_audio:
                continue
            lines.append(
                f"{index:03d}  {reel:<8} {channel:<5} C        "
                f"{rate.timecode(clip.segment.source_in)} {rate.timecode(clip.segment.source_out)} "
                f"{rate.timecode(record_in)} {rate.timecode(record_out)}"
            )
        lines.append(f"* FROM CLIP NAME: {info.path.name}")
        if clip.segment.gain_db:
            lines.append(f"* AUDIO LEVEL: {clip.segment.gain_db:+.1f} DB")
        lines.append("")

    return "\n".join(lines) + "\n"


def write_edl(timeline: Timeline, path: Path, *, title: str = "precut") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_edl(timeline, title=title), encoding="utf-8")
    return path
