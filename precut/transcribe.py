"""음성 인식(STT) 래퍼.

faster-whisper → openai-whisper 순으로 사용 가능한 백엔드를 고른다.
둘 다 없으면 자막 단계만 건너뛰고 컷/밸런스는 그대로 진행한다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


class TranscriptionUnavailable(RuntimeError):
    """STT 백엔드가 설치되어 있지 않음."""


@dataclass
class Word:
    start: float
    end: float
    text: str
    probability: float = 1.0


@dataclass
class Utterance:
    start: float
    end: float
    text: str
    words: list[Word] = field(default_factory=list)


@dataclass
class Transcript:
    utterances: list[Utterance]
    language: str = ""
    backend: str = ""

    @property
    def has_word_times(self) -> bool:
        return any(u.words for u in self.utterances)

    def all_words(self) -> list[Word]:
        words: list[Word] = []
        for utterance in self.utterances:
            words.extend(utterance.words)
        return words


@dataclass
class TranscribeOptions:
    model: str = "medium"
    language: str | None = "ko"
    device: str = "auto"
    compute_type: str = "auto"
    beam_size: int = 5
    vad_filter: bool = True
    initial_prompt: str | None = None


def available_backend() -> str | None:
    try:
        import faster_whisper  # noqa: F401

        return "faster-whisper"
    except Exception:
        pass
    try:
        import whisper  # noqa: F401

        return "openai-whisper"
    except Exception:
        return None


def transcribe(audio_path: Path, options: TranscribeOptions, *, progress=None) -> Transcript:
    backend = available_backend()
    if backend == "faster-whisper":
        return _transcribe_faster_whisper(audio_path, options, progress)
    if backend == "openai-whisper":
        return _transcribe_openai_whisper(audio_path, options)
    raise TranscriptionUnavailable(
        "자막 생성을 위해 STT 백엔드가 필요합니다. `pip install faster-whisper` 를 실행하거나 "
        "--no-subtitles 로 자막 단계를 끄세요."
    )


def _transcribe_faster_whisper(audio_path: Path, options: TranscribeOptions, progress=None) -> Transcript:
    from faster_whisper import WhisperModel

    device = options.device
    compute_type = options.compute_type
    if device == "auto":
        device = "cuda" if _cuda_available() else "cpu"
    if compute_type == "auto":
        compute_type = "float16" if device == "cuda" else "int8"

    model = WhisperModel(options.model, device=device, compute_type=compute_type)
    segments, info = model.transcribe(
        str(audio_path),
        language=options.language,
        beam_size=options.beam_size,
        word_timestamps=True,
        vad_filter=options.vad_filter,
        initial_prompt=options.initial_prompt,
        condition_on_previous_text=False,
    )

    utterances: list[Utterance] = []
    for segment in segments:
        words = [
            Word(start=w.start, end=w.end, text=w.word, probability=getattr(w, "probability", 1.0))
            for w in (segment.words or [])
            if w.start is not None and w.end is not None
        ]
        utterances.append(
            Utterance(start=segment.start, end=segment.end, text=segment.text.strip(), words=words)
        )
        if progress:
            progress(segment.end, getattr(info, "duration", 0.0) or 0.0)

    return Transcript(
        utterances=utterances,
        language=getattr(info, "language", options.language or ""),
        backend="faster-whisper",
    )


def _transcribe_openai_whisper(audio_path: Path, options: TranscribeOptions) -> Transcript:
    import whisper

    model = whisper.load_model(options.model)
    result = model.transcribe(
        str(audio_path),
        language=options.language,
        word_timestamps=True,
        initial_prompt=options.initial_prompt,
        verbose=False,
    )

    utterances: list[Utterance] = []
    for segment in result.get("segments", []):
        words = [
            Word(
                start=float(w["start"]),
                end=float(w["end"]),
                text=str(w.get("word", "")),
                probability=float(w.get("probability", 1.0)),
            )
            for w in segment.get("words", [])
            if w.get("start") is not None and w.get("end") is not None
        ]
        utterances.append(
            Utterance(
                start=float(segment["start"]),
                end=float(segment["end"]),
                text=str(segment.get("text", "")).strip(),
                words=words,
            )
        )

    return Transcript(
        utterances=utterances,
        language=str(result.get("language") or options.language or ""),
        backend="openai-whisper",
    )


def _cuda_available() -> bool:
    try:
        import torch

        return bool(torch.cuda.is_available())
    except Exception:
        return False
