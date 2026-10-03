"""Heuristic quality gate over the existing docling blob (spec §8: log-only first).

Thresholds are INITIAL heuristics — calibrate with real traffic (Phase C metrics)
before enabling fallback (Phase F).
"""
import re
from dataclasses import dataclass
from typing import Optional

# Markdown image line covering the whole line: ![alt](path)
_IMAGE_PLACEHOLDER_RE = re.compile(r"^\s*!\[[^\]]*\]\([^)]*\)\s*$", re.MULTILINE)
# Control chars, U+FFFD replacement char, or PDF cid: glyphs from broken encodings
_GARBAGE_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\ufffd]|\(cid:\d+\)")
# Any character outside a conservative printable whitelist
_NON_PRINTABLE_RE = re.compile(r"[^\w\s.,;:!?()«»\"'¿¡%€$\-–—/\\@#*&+=<>{}\[\]|~^`°]")


@dataclass
class GateSignals:
    page_count: Optional[int]
    char_count: int
    chars_per_page: float
    text_density: float
    image_placeholder_ratio: float
    garbage_ratio: float


def compute_signals(text: str, page_count: Optional[int]) -> GateSignals:
    n = max(page_count or 0, 1)
    char_count = len(text)
    lines = text.splitlines() or [""]
    placeholders = len(_IMAGE_PLACEHOLDER_RE.findall(text))
    garbage = len(_GARBAGE_RE.findall(text))
    non_printable = len(_NON_PRINTABLE_RE.findall(text))
    return GateSignals(
        page_count=page_count,
        char_count=char_count,
        chars_per_page=char_count / n,
        text_density=1.0 - non_printable / max(char_count, 1),
        image_placeholder_ratio=placeholders / len(lines),
        garbage_ratio=(garbage + non_printable) / max(char_count, 1),
    )


def evaluate_document(s: GateSignals, settings) -> str:
    """Document-level decision: "pass" | "suspect"."""
    if s.char_count == 0:
        return "suspect"
    if s.chars_per_page < settings.min_chars_per_page:
        return "suspect"
    if s.image_placeholder_ratio > settings.image_placeholder_ratio_max:
        return "suspect"
    if s.garbage_ratio > settings.garbage_ratio_max:
        return "suspect"
    return "pass"


def evaluate_page(page_text, settings) -> str:
    """Page-level decision: "pass" | "suspect" (used only in slow path, Phase E)."""
    if page_text is None:  # branch none (A.1)
        return "suspect"
    if len(page_text.strip()) < settings.min_chars_per_page:
        return "suspect"
    return "pass"
