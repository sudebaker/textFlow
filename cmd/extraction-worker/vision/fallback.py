"""Two-level gate + optional Vision OCR fallback (spec §3: contract intact).

run_quality_flow() is the ONLY entry point the worker calls.

Fast path (gate PASS, or OCR disabled): returns ctx.text UNCHANGED — no
rendering, no OCR calls, no provenance writes (golden test enforces
byte-identical output with VISION disabled).

Slow path (gate SUSPECT + OCR enabled): renders suspect pages, asks
vision-ocr, REPLACES thin pages (§15: replacement, never fusion), honors the
per-document budget (§19) and writes per-page provenance (spec §17). Vision
errors NEVER kill the job (§16): the page keeps its docling text and the
report records vision_error.
"""

import asyncio
import json
import logging
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Optional

import aiohttp

# NOTE: this package lives under cmd/extraction-worker — the worker runs with
# its own directory in sys.path, so `from vision.fallback import ...` resolves.
from .client import VisionOCRClient
from .config import SETTINGS, VisionSettings
from .gate import compute_signals, evaluate_document, evaluate_page
from .metrics import (
    quality_gate_documents_total,
    vision_documents_total,
    vision_page_seconds,
    vision_pages_total,
)
from .provenance import write_provenance
from .renderer import render_page_async

logger = logging.getLogger(__name__)

# Fase A decision (docs/extraccion-visual-verificacion-A.md A.1): per-page
# text comes from the docling document JSON `texts[].prov[0].page_no`.
PAGE_TEXT_MODE = "texts_prov"  # "texts_prov" | "none"


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


def _page_provenance(page_no: int, backend: str, status: str, reason: str = None) -> dict:
    entry = {"page": page_no, "backend": backend, "status": status}
    if reason:
        entry["reason"] = reason
    return entry


@dataclass
class _Budget:
    """Per-document budget (spec §19): counts VISION pages, not page indexes."""

    max_pages: int
    max_seconds: float
    start: float = field(default_factory=time.monotonic)

    @property
    def used_pages(self) -> int:
        return self._used

    def allow(self) -> bool:
        if self._used >= self.max_pages:
            return False
        return self.elapsed() <= self.max_seconds

    def spend(self) -> None:
        self._used += 1

    def elapsed(self) -> float:
        return time.monotonic() - self.start


def _init_budget(max_pages: int, max_seconds: float) -> _Budget:
    budget = _Budget(max_pages, max_seconds)
    budget._used = 0
    return budget


def _unwrap_json_content(docling_document: dict):
    """Return the texts list for PAGE_TEXT_MODE=texts_prov (A.1 shape).

    Priority: `json_content` (the DoclingDocument; docling-serve only fills it
    with to_formats=json — A.1) over a top-level `texts`. json_content may be
    a dict OR a JSON string — both accepted defensively.
    """
    jc = docling_document.get("json_content")
    if jc is not None:
        if isinstance(jc, str):
            try:
                jc = json.loads(jc)
            except ValueError:
                logger.debug("docling json_content is a non-JSON string; treating as no per-page texts")
                return None
        if isinstance(jc, dict):
            texts = jc.get("texts")
            if isinstance(texts, list):
                return texts
    texts = docling_document.get("texts")
    if isinstance(texts, list):
        return texts
    return None


def extract_page_texts(docling_document: dict, page_count: int) -> list:
    """Per-page docling text (texts_prov mode: A.1).

    Groups document texts by prov[0].page_no, joining per-page items with
    "\n". A page without items (or blank-only) -> None: the page gate marks
    it suspect, which is the safe degrade (never loses text).
    Unknown shapes -> [None]*page_count (page gate marks all suspect).
    """
    if PAGE_TEXT_MODE == "none" or not docling_document:
        return [None] * page_count
    texts = _unwrap_json_content(docling_document)
    if texts is None:
        logger.debug("docling document has no per-page texts (unknown shape); all pages suspect")
        return [None] * page_count
    pages: dict = {i: [] for i in range(1, page_count + 1)}
    for item in texts:
        if not isinstance(item, dict):
            continue
        prov = (item.get("prov") or [{}])[0]
        idx = prov.get("page_no") if isinstance(prov, dict) else None
        if isinstance(idx, int) and 1 <= idx <= page_count:
            pages[idx].append(item.get("text") or "")
    return ["\n".join(parts).strip() or None for parts in
            (pages[i] for i in range(1, page_count + 1))]


async def run_quality_flow(ctx: FallbackContext) -> str:
    """Quality gate + (optional) vision fallback over the docling blob.

    Fast path returns ctx.text unchanged (invariants 1+2). Slow path runs
    only when gate_enabled AND ocr_enabled AND decision==suspect. Vision
    failures never kill the job (§16) — JobCancelledError propagates (it is
    the cooperative-cancel policy, not a vision failure).
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
    if decision == "pass" or not ctx.settings.ocr_enabled:
        if decision == "pass":
            vision_documents_total.labels(path="fast_path").inc()
        return ctx.text  # byte-identical (invariants 1+2)
    try:
        from pkg.shared.exceptions import JobCancelledError

        return await _slow_path(ctx, signals, decision)
    except JobCancelledError:
        raise  # cooperative cancellation propagates (worker handles it)
    except Exception as e:
        logger.error(
            "vision fallback failed job=%s: %s — keeping docling output",
            ctx.job_id,
            e,
        )
        return ctx.text  # §16: vision NEVER kills the job


def _write_report(ctx: FallbackContext, decision: str, signals, report: list,
                  budget: _Budget, duration_s: float, whole_fallback: bool) -> None:
    write_provenance(
        ctx.redis_client,
        ctx.job_id,
        {
            "gate": {"decision": decision, **asdict(signals)},
            "pages": report,
            "vision_pages": budget.used_pages,
            "duration_s": round(duration_s, 2),
            "fallback_whole_document": whole_fallback_flag(whole_fallback),
        },
    )


def whole_fallback_flag(flag: bool) -> bool:
    return flag


async def _slow_path(ctx: FallbackContext, signals, decision: str) -> str:
    s = ctx.settings
    ctx.raise_if_cancelled(ctx.job_id)
    pdf_bytes = await asyncio.to_thread(ctx.get_document_bytes)  # lazy, ONCE (invariant 3)
    if pdf_bytes[:4] != b"%PDF":
        return ctx.text  # scope §26: only paginated PDFs
    page_texts = extract_page_texts(ctx.docling_document, ctx.page_count)
    budget = _init_budget(s.max_pages_per_document, s.max_seconds_per_document)
    final, report, any_vision = [], [], False
    client = VisionOCRClient(s)
    start = time.monotonic()
    async with aiohttp.ClientSession() as session:
        for page_no in range(1, ctx.page_count + 1):
            ctx.raise_if_cancelled(ctx.job_id)  # §18: between pages
            page_text = page_texts[page_no - 1]
            if evaluate_page(page_text, s) == "pass":
                final.append(page_text or "")
                report.append(_page_provenance(page_no, "docling", "pass"))
                continue
            if not budget.allow():
                final.append(page_text or "")  # §19: keep available text
                report.append(_page_provenance(page_no, "docling", "degraded",
                                               reason="budget_exhausted"))
                vision_pages_total.labels(backend="degraded").inc()
                continue
            t0 = time.monotonic()
            try:  # §16: narrow try/except
                image = await render_page_async(pdf_bytes, page_no, dpi=s.image_dpi)
                vision_text = await client.transcribe(session, image, dpi=s.image_dpi)
                vision_page_seconds.observe(time.monotonic() - t0)
                if vision_text.strip():  # §15: replacement, never fusion
                    final.append(vision_text)
                    any_vision = True
                    report.append(_page_provenance(page_no, "vision", "fallback",
                                                   reason="quality_gate"))
                    vision_pages_total.labels(backend="vision").inc()
                    budget.spend()
                else:  # empty transcription: keep docling text
                    final.append(page_text or "")
                    report.append(_page_provenance(page_no, "docling", "vision_empty"))
                    vision_pages_total.labels(backend="docling").inc()
            except Exception as e:
                vision_page_seconds.observe(time.monotonic() - t0)
                final.append(page_text or "")
                report.append(_page_provenance(page_no, "vision_error", "vision_error",
                                               reason=str(e)[:200]))
                vision_pages_total.labels(backend="vision_error").inc()
    assembled = "\n\n".join(final).strip()
    whole_fallback = False
    if not assembled:
        assembled = ctx.text  # §16 last defense: intact docling blob
        whole_fallback = True
    slow_engaged = any_vision or any(
        entry["status"] != "pass" for entry in report
    )
    if slow_engaged:
        # Provenance records slow-path outcomes: replaced pages, vision
        # errors (§16: provenance vision_error even when text is kept) and
        # degraded pages. Text output: replaced -> assembled; otherwise
        # byte-exact ctx.text (no vision page replaced anything).
        if any_vision:
            _write_report(ctx, decision, signals, report, budget,
                          time.monotonic() - start, whole_fallback)
            vision_documents_total.labels(path="slow_path").inc()
            return assembled
        _write_report(ctx, decision, signals, report, budget,
                      time.monotonic() - start, whole_fallback)
        return ctx.text
    return ctx.text
