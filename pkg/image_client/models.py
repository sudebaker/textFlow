from dataclasses import dataclass
from typing import Optional


@dataclass
class ImageAnalysisResult:
    extracted_text: str
    language: Optional[str] = None
    description: Optional[str] = None
    confidence: Optional[float] = None