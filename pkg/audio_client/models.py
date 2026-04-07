from dataclasses import dataclass
from typing import Optional


@dataclass
class AudioSegment:
    start: float
    end: float
    text: str
    speaker: Optional[str] = None


@dataclass
class TranscriptionResult:
    text: str
    language: Optional[str] = None
    duration_seconds: Optional[float] = None
    segments: Optional[list[AudioSegment]] = None