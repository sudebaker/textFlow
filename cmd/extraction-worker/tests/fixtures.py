"""Deterministic minimal-PDF builder (no binary fixtures in git).

Pure-Python, stdlib-only. Builds a valid PDF 1.4 document with one page per
input string so extraction tests exercise the real parser with predictable
content. Object layout for n pages:

    1      catalog (/Type /Catalog, /Pages 2 0 R)
    2      page tree (/Type /Pages, Kids 3..n*2+2, Count n)
    3+2i   page i (/Type /Page, MediaBox A4-ish 612x792, /Contents + /F1)
    4+2i   content stream of page i (BT /F1 24 Tf 72 720 Td (text) Tj ET)
    4+2n+1 shared font object (/Type /Font Helvetica — first id after pages)
"""

import hashlib

HEADER = b"%PDF-1.4"

# Shared default appearance for every page's text operator.
_FONT_LINE = b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"
_CONTENT_OP = b"BT /F1 24 Tf 72 720 Td ({txt}) Tj ET"


def _escape(text: str) -> bytes:
    """Escape a PDF literal string (backslash and parenthesis)."""
    return (
        text.replace("\\", "\\\\")
        .replace("(", "\\(")
        .replace(")", "\\)")
        .encode("utf-8")
    )


def _serialize_object(obj_num: int, body: bytes) -> bytes:
    return b"%d 0 obj\n" % obj_num + body + b"\nendobj\n"


def _build_stream(text: str) -> bytes:
    stream = _CONTENT_OP.replace(b"{txt}", _escape(text))
    return b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream"


def build_pdf(page_texts: list) -> bytes:
    """Build a minimal valid PDF with one page per entry of ``page_texts``.

    Args:
        page_texts: One non-empty list entry per page; the string is drawn
            with the shared Helvetica font at the page origin area.

    Returns:
        The complete PDF as bytes: %PDF-1.4 header, all objects, xref table
        with true byte offsets, trailer with /Size /Root, startxref, %%EOF.
    """
    if not page_texts:
        raise ValueError("page_texts must contain at least one page")

    n = len(page_texts)
    pages_obj = 2
    font_obj = 3 + 2 * n  # first free id after the 2n Page/Contents objects
    total_objects = font_obj  # == /Size - 1 (obj 0 is the free head)

    # Body bytes, in ascending object-number order.
    pieces = [_serialize_object(1, b"<< /Type /Catalog /Pages 2 0 R >>")]
    pieces.append(
        _serialize_object(
            pages_obj,
            b"<< /Type /Pages /Kids [" + b" ".join(
                b"%d 0 R" % (3 + 2 * i) for i in range(n)
            ) + b"] /Count %d >>" % n,
        )
    )
    for i, text in enumerate(page_texts):
        pieces.append(_serialize_object(3 + 2 * i, b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 %d 0 R >> >> /Contents %d 0 R >>" % (font_obj, 4 + 2 * i)))
        pieces.append(_serialize_object(4 + 2 * i, _build_stream(text)))

    # Shared font, referenced by every page's /Resources.
    pieces.append(_serialize_object(font_obj, _FONT_LINE))

    # xref table: offsets computed over header + all body pieces so far.
    offsets = [0]  # obj 0
    offset = len(HEADER) + 1  # header plus the \n after it
    for piece in pieces:
        offsets.append(offset)
        offset += len(piece)
    sections = [
        b"xref\n0 %d\n" % (total_objects + 1),
        b"0000000000 65535 f \n",
    ]
    for off in offsets[1:]:
        sections.append(b"%010d 00000 n \n" % off)
    trailer = (
        b"trailer << /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF" % (
            total_objects + 1,
            offset,
        )
    )

    return HEADER + b"\n" + b"".join(pieces) + b"".join(sections) + trailer


def fixture_sha256(page_texts: list) -> str:
    """SHA-256 of ``build_pdf(page_texts)`` (test convenience)."""
    return hashlib.sha256(build_pdf(page_texts)).hexdigest()
