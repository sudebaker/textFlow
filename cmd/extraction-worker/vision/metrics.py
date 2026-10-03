"""Vision metrics (spec §27): naming convention `extraction_worker_*`."""

from prometheus_client import Counter, Histogram

quality_gate_documents_total = Counter(
    "extraction_worker_quality_gate_documents_total",
    "Document-level gate decisions",
    ["decision"],  # pass | suspect
)
vision_documents_total = Counter(
    "extraction_worker_vision_documents_total",
    "Documents by path taken",
    ["path"],  # fast_path | slow_path
)
vision_pages_total = Counter(
    "extraction_worker_vision_pages_total",
    "Pages by final backend",
    ["backend"],  # docling | vision | vision_error | degraded
)
vision_page_seconds = Histogram(
    "extraction_worker_vision_page_seconds",
    "Per-page vision processing time (s)",
)
vision_document_seconds = Histogram(
    "extraction_worker_vision_document_seconds",
    "Slow-path document time (s)",
)
