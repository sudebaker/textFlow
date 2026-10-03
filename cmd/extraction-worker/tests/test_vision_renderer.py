"""Tests for vision.renderer: pypdfium2 page materialization (spec §7, slow path only)."""

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

# The golden test (alphabetically earlier, once the whole tests dir runs)
# unconditionally stubs pypdfium2/PIL/PIL.Image as MagicMocks in sys.modules.
# Restore the real modules so these tests exercise the actual renderer.
# Standalone runs are unaffected (KeyError pass-through).
for _mod in ("pypdfium2", "PIL", "PIL.Image"):
    sys.modules.pop(_mod, None)

pytest.importorskip("pypdfium2")
pytest.importorskip("PIL")

from vision.renderer import count_pages, render_page, render_page_async  # noqa: E402
from fixtures import build_pdf  # noqa: E402

PNG_MAGIC = b"\x89PNG"


def _png_dims(png_bytes):
    from PIL import Image
    import io

    with Image.open(io.BytesIO(png_bytes)) as im:
        return im.size


class TestCountPages:
    def test_two_pages(self):
        assert count_pages(build_pdf(["a", "b"])) == 2

    def test_one_page(self):
        assert count_pages(build_pdf(["only"])) == 1


class TestRenderPage:
    def test_png_magic_bytes(self):
        pdf = build_pdf(["hello page"])
        assert render_page(pdf, 1)[:4] == PNG_MAGIC

    def test_render_is_1_indexed(self):
        pdf = build_pdf(["a", "b"])
        # Page 1 and 2 both renderable; page 3 raises.
        assert render_page(pdf, 2)[:4] == PNG_MAGIC
        with pytest.raises(IndexError):
            render_page(pdf, 3)

    def test_dpi_scales_image_size(self):
        pdf = build_pdf(["dpi comparison"])
        small = _png_dims(render_page(pdf, 1, dpi=120))
        big = _png_dims(render_page(pdf, 1, dpi=300))
        assert big[0] > small[0]
        assert big[1] > small[1]


class TestRenderPageErrors:
    def test_page_zero_raises(self):
        pdf = build_pdf(["a"])
        with pytest.raises(IndexError):
            render_page(pdf, 0)

    def test_out_of_range_raises(self):
        pdf = build_pdf(["a"])
        with pytest.raises(IndexError):
            render_page(pdf, 5)


class TestRenderPageAsync:
    def test_async_same_png_magic(self):
        pdf = build_pdf(["async page"])
        result = asyncio.run(render_page_async(pdf, 1))
        assert result[:4] == PNG_MAGIC
