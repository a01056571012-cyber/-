"""편집 대본 파일 읽기/쓰기.

무엇을 남길지는 결국 사람이 정해야 한다. 프로그램은 후보를 대본으로 뽑아주고,
사용자가 지울 줄을 표시해서 다시 넣으면 그대로 편집본이 만들어진다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .timecode import parse_duration

HEADER = """\
# precut 편집 대본
#
# 남기고 싶지 않은 줄은 맨 앞에 - 를 붙이거나 줄을 통째로 지우세요.
# 시간은 원본 영상 기준입니다. 시간을 직접 고쳐도 됩니다.
# 다 고쳤으면 이 파일을 --script 옵션으로 넣어 다시 실행하세요.
#
#   precut <영상> --merge --script "이파일.txt"
#
# 형식: [번호] | 파일 | 시작 - 끝 | 길이 | 내용
"""

LINE = re.compile(
    r"^\s*(?P<drop>[-#])?\s*\[(?P<index>\d+)\]\s*\|"
    r"\s*(?P<source>[^|]+?)\s*\|"
    r"\s*(?P<start>[\d:.]+)\s*-\s*(?P<end>[\d:.]+)\s*\|"
    r"(?P<rest>.*)$"
)


@dataclass
class ScriptEntry:
    index: int
    source: str
    start: float
    end: float
    text: str = ""

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)


def _stamp(seconds: float) -> str:
    seconds = max(0.0, seconds)
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    return "%02d:%02d:%05.2f" % (hours, minutes, secs)


def render_script(entries: list[ScriptEntry]) -> str:
    lines = [HEADER]
    for entry in entries:
        lines.append(
            f"[{entry.index:03d}] | {entry.source} | "
            f"{_stamp(entry.start)} - {_stamp(entry.end)} | "
            f"{entry.duration:5.1f}초 | {entry.text}".rstrip()
        )
    return "\n".join(lines) + "\n"


def parse_script(text: str) -> list[ScriptEntry]:
    """대본에서 살아남은 줄만 읽는다. '-'나 '#'로 시작하는 줄은 버린 것으로 본다."""
    entries: list[ScriptEntry] = []
    for raw in text.splitlines():
        if not raw.strip():
            continue
        match = LINE.match(raw)
        if not match or match.group("drop"):
            continue
        try:
            start = parse_duration(match.group("start"))
            end = parse_duration(match.group("end"))
        except ValueError:
            continue
        if end <= start:
            continue
        rest = match.group("rest")
        _, _, comment = rest.partition("|")
        entries.append(
            ScriptEntry(
                index=int(match.group("index")),
                source=match.group("source").strip(),
                start=start,
                end=end,
                text=comment.strip(),
            )
        )
    return entries


def write_script(entries: list[ScriptEntry], path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_script(entries), encoding="utf-8")
    return path


def read_script(path: Path) -> list[ScriptEntry]:
    entries = parse_script(Path(path).read_text(encoding="utf-8"))
    if not entries:
        raise ValueError(f"대본에서 남길 구간을 찾지 못했습니다: {path}")
    return entries


def group_by_source(entries: list[ScriptEntry]) -> dict[str, list[tuple[float, float]]]:
    grouped: dict[str, list[tuple[float, float]]] = {}
    for entry in entries:
        grouped.setdefault(entry.source, []).append((entry.start, entry.end))
    for spans in grouped.values():
        spans.sort()
    return grouped
