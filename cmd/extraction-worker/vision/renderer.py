"""PDF page materialization via pypdfium2 — SLOW PATH ONLY (spec §7).
Fast path must never render (golden test enforces)."""

import asyncio
import io
import logging

logger = logging.getLogger(__name__)


def _pdfium():  # lazy import: air-gapped test envs may lack it
    import pypdfium2 as pdfium

    return pdfium


def count_pages(pdf_bytes: bytes) -> int:
    doc = _pdfium().PdfDocument(pdf_bytes)
    try:
        return len(doc)
    finally:
        doc.close()


def render_page(pdf_bytes: bytes, page_number: int, *, dpi: int = 200) -> bytes:
    """Render 1-indexed page to PNG bytes. scale=dpi/72; orientation preserved."""
    doc = _pdfium().PdfDocument(pdf_bytes)
    try:
        # Explicit bounds check: pypdfium2 raises PdfiumError, not IndexError.
        if page_number < 1 or page_number > len(doc):
            raise IndexError(f"page_number {page_number} out of range (1..{len(doc)})")
        bitmap = doc[page_number - 1].render(scale=dpi / 72.0)
        buf = io.BytesIO()
        bitmap.to_pil().save(buf, format="PNG")
        return buf.getvalue()
    finally:
        doc.close()


async def render_page_async(pdf_bytes: bytes, page_number: int, *, dpi: int = 200) -> bytes:
    return await asyncio.to_thread(render_page, pdf_bytes, page_number, dpi=dpi)
