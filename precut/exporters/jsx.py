"""프리미어 프로 ExtendScript(.jsx) 생성.

프리미어에서 `파일 > 스크립트 > 스크립트 파일 실행`으로 이 파일을 고르면
XML 시퀀스와 SRT 자막을 한 번에 프로젝트로 불러온다.
"""

from __future__ import annotations

import json
from pathlib import Path

TEMPLATE = r"""// precut - 자동 생성 스크립트
// 프리미어 프로: 파일 > 스크립트 > 스크립트 파일 실행... 에서 이 파일을 선택하세요.
(function () {
    var CONFIG = __CONFIG__;

    function log(lines, message) {
        lines.push(message);
        if ($.writeln) { $.writeln(message); }
    }

    function existing(paths) {
        var found = [];
        for (var i = 0; i < paths.length; i++) {
            if (paths[i] && new File(paths[i]).exists) { found.push(paths[i]); }
        }
        return found;
    }

    function findBin(name) {
        var root = app.project.rootItem;
        for (var i = 0; i < root.children.numItems; i++) {
            var item = root.children[i];
            if (item.name === name && item.type === ProjectItemType.BIN) { return item; }
        }
        return root.createBin(name);
    }

    function findSequenceByName(name) {
        for (var i = 0; i < app.project.sequences.numSequences; i++) {
            var sequence = app.project.sequences[i];
            if (sequence.name === name) { return sequence; }
        }
        return null;
    }

    function findProjectItem(node, predicate) {
        for (var i = 0; i < node.children.numItems; i++) {
            var item = node.children[i];
            if (predicate(item)) { return item; }
            if (item.type === ProjectItemType.BIN) {
                var nested = findProjectItem(item, predicate);
                if (nested) { return nested; }
            }
        }
        return null;
    }

    var messages = [];

    if (!app.project) {
        alert("열려 있는 프리미어 프로젝트가 없습니다.");
        return;
    }

    var bin = findBin(CONFIG.binName);
    var toImport = existing([CONFIG.xmlPath, CONFIG.srtPath, CONFIG.audioPath]);
    if (toImport.length === 0) {
        alert("가져올 파일을 찾지 못했습니다:\n" + CONFIG.xmlPath);
        return;
    }

    app.project.importFiles(toImport, true, bin, false);
    log(messages, "가져온 파일 " + toImport.length + "개");

    var sequence = findSequenceByName(CONFIG.sequenceName);
    if (sequence) {
        app.project.openSequence(sequence.sequenceID);
        log(messages, "시퀀스 열기: " + sequence.name);
    } else {
        log(messages, "시퀀스를 찾지 못했습니다. 프로젝트 패널에서 직접 열어주세요.");
    }

    if (CONFIG.srtPath) {
        var srtName = CONFIG.srtName;
        var caption = findProjectItem(app.project.rootItem, function (item) {
            return item.name === srtName;
        });
        if (caption && sequence) {
            var placed = false;
            try {
                var track = sequence.videoTracks[sequence.videoTracks.numTracks - 1];
                placed = track.overwriteClip(caption, 0);
            } catch (err) {
                placed = false;
            }
            log(messages, placed
                ? "자막 트랙을 타임라인에 배치했습니다."
                : "자막 파일을 가져왔습니다. 프로젝트 패널에서 타임라인으로 끌어놓으세요.");
        }
    }

    alert("precut\n\n" + messages.join("\n"));
})();
"""


def render_jsx(
    *,
    xml_path: Path,
    sequence_name: str,
    srt_path: Path | None = None,
    audio_path: Path | None = None,
    bin_name: str = "precut",
) -> str:
    config = {
        "xmlPath": str(xml_path.resolve()),
        "srtPath": str(srt_path.resolve()) if srt_path else "",
        "srtName": srt_path.name if srt_path else "",
        "audioPath": str(audio_path.resolve()) if audio_path else "",
        "sequenceName": sequence_name,
        "binName": bin_name,
    }
    return TEMPLATE.replace("__CONFIG__", json.dumps(config, ensure_ascii=False, indent=8))


def write_jsx(path: Path, **kwargs) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_jsx(**kwargs), encoding="utf-8")
    return path
