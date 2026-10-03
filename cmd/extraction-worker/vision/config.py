"""Env-driven settings for the visual extraction flow (spec §22). os.getenv only."""
import os
from dataclasses import dataclass

def _flag(name, default="false"):
    return os.getenv(name, default).lower() in ("1", "true", "yes")


@dataclass(frozen=True)
class VisionSettings:
    ocr_enabled: bool
    gate_enabled: bool
    ocr_url: str
    ocr_timeout: float
    max_pages_per_document: int
    max_seconds_per_document: float
    max_concurrency: int
    image_dpi: int
    min_chars_per_page: int
    image_placeholder_ratio_max: float
    garbage_ratio_max: float

    @classmethod
    def load(cls):
        return cls(
            ocr_enabled=_flag("VISION_OCR_ENABLED"),  # default false — spec §14
            gate_enabled=_flag("VISION_GATE_ENABLED", "true"),  # Fase C log-only
            ocr_url=os.getenv("VISION_OCR_URL", "http://vision-ocr:8080"),
            ocr_timeout=float(os.getenv("VISION_OCR_TIMEOUT", "120")),
            max_pages_per_document=int(os.getenv("VISION_OCR_MAX_PAGES_PER_DOCUMENT", "50")),
            max_seconds_per_document=float(os.getenv("VISION_OCR_MAX_SECONDS_PER_DOCUMENT", "900")),
            max_concurrency=int(os.getenv("VISION_OCR_MAX_CONCURRENCY", "4")),
            image_dpi=int(os.getenv("VISION_OCR_IMAGE_DPI", "200")),
            min_chars_per_page=int(os.getenv("VISION_MIN_CHARS_PER_PAGE", "100")),
            image_placeholder_ratio_max=float(os.getenv("VISION_IMAGE_PLACEHOLDER_RATIO_MAX", "0.3")),
            garbage_ratio_max=float(os.getenv("VISION_GARBAGE_RATIO_MAX", "0.2")),
        )


SETTINGS = VisionSettings.load()  # singleton; tests pass explicit settings
