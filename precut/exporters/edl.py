"""CMX3600 EDL 출력 (다빈치 리졸브·구형 NLE용 대체 경로)."""

from __future__ import annotations

from pathlib import Path

from ..media import MediaInfo
from ..segments import CutPlan


def render_edl(plan: CutPlan, info: MediaInfo, *, title: str = "precut") -> str:
    rate = plan.frame_rate
    lines = [f"TITLE: {title}", "FCM: " + ("DROP FRAME" if rate.ntsc else "NON-DROP FRAME"), ""]
    record = 0.0
    reel = (info.path.stem[:8] or "AX").upper().replace(" ", "_")

    for index, segment in enumerate(plan.segments, start=1):
        record_end = record + segment.duration
        for channel in ("V", "A"):
            if channel == "V" and not info.has_video:
                continue
            if channel == "A" and not info.has_audio:
                continue
            lines.append(
                f"{index:03d}  {reel:<8} {channel:<5} C        "
                f"{rate.timecode(segment.source_in)} {rate.timecode(segment.source_out)} "
                f"{rate.timecode(record)} {rate.timecode(record_end)}"
            )
        lines.append(f"* FROM CLIP NAME: {info.path.name}")
        if segment.gain_db:
            lines.append(f"* AUDIO LEVEL: {segment.gain_db:+.1f} DB")
        lines.append("")
        record = record_end

    return "\n".join(lines) + "\n"


def write_edl(plan: CutPlan, info: MediaInfo, path: Path, *, title: str = "precut") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_edl(plan, info, title=title), encoding="utf-8")
    return path
