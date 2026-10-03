"""Two-level gate + optional Vision OCR fallback (spec §3: contract intact).

run_quality_flow() is the ONLY entry point the worker calls.

Phase C (log-only): the gate only observes — it increments counters, logs the
decision and returns ctx.text UNCHANGED. No Redis writes, no rendering, no OCR
calls. The golden test (test_golden_vision_off.py) enforces byte-identical
output with VISION disabled.
"""
import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

# NOTE: this package lives under cmd/extraction-worker — the worker runs with
# its own directory in sys.path, so `from vision.fallback import ...` resolves.
from .config import SETTINGS, VisionSettings
from .gate import compute_signals, evaluate_document
from .metrics import quality_gate_documents_total, vision_documents_total

logger = logging.getLogger(__name__)


@dataclass
class FallbackContext:
    job_id: str
    text: str
    docling_document: dict
    page_count: Optional[int]
    get_document_bytes: Callable[[], bytes]  # lazy: only called on slow path
    redis_client: Any
    raise_if_cancelled: Callable[[str], None]
    settings: VisionSettings = field(default_factory=lambda: SETTINGS)


async def run_quality_flow(ctx: FallbackContext) -> str:
    """Log-only quality gate over the docling blob (Phase C).

    Returns ctx.text UNCHANGED — always, in this phase. SUSPECT decisions are
    only recorded (log + metrics); fallback activation belongs to Phase E.
    """
    if not (ctx.settings.gate_enabled or ctx.settings.ocr_enabled):
        return ctx.text
    signals = compute_signals(ctx.text, ctx.page_count)
    decision = evaluate_document(signals, ctx.settings)
    quality_gate_documents_total.labels(decision=decision).inc()
    logger.info(
        "quality gate job=%s decision=%s chars/page=%.0f img_ratio=%.2f garbage=%.2f",
        ctx.job_id,
        decision,
        signals.chars_per_page,
        signals.image_placeholder_ratio,
        signals.garbage_ratio,
    )
    if decision == "pass":
        vision_documents_total.labels(path="fast_path").inc()
    # Phase C log-only: SUSPECT is only recorded, nothing is altered
    return ctx.text  # byte-identical ALWAYS in this phase
