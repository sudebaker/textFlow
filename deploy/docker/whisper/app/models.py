from typing import List
from pydantic import BaseModel, Field


class Segment(BaseModel):
    id: int = Field(0, description="Segment ID")
    start: float = Field(..., description="Start time in seconds")
    end: float = Field(..., description="End time in seconds")
    text: str = Field(..., description="Segment text")
    avg_logprob: float = Field(0.0, description="Average log probability")


class TranscribeResponse(BaseModel):
    language: str = Field(..., description="Detected language")
    duration: float = Field(..., description="Audio duration in seconds")
    segments: List[Segment]
    text: str = Field(..., description="Full transcription text")


class HealthResponse(BaseModel):
    status: str
    device: str
    model: str
    ready: bool