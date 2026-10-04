"""HTTP client for the vision-ocr service (spec §9/§20: client-side limit).

The semaphore keeps concurrent page transcriptions bounded from the worker
side as well — the vision-ocr service additionally enforces a server-side
semaphore and returns 503 when saturated (spec §20, two-sided protection).
"""

import asyncio
import logging

import aiohttp

logger = logging.getLogger(__name__)


class VisionSaturatedError(RuntimeError):
    """vision-ocr returned 503 — the page falls back to docling text."""


class VisionHTTPError(RuntimeError):
    """vision-ocr returned a non-200/503 status."""


class VisionOCRClient:
    def __init__(self, settings):
        self.settings = settings
        self._sem = asyncio.Semaphore(settings.max_concurrency)
        # Kept lazy: under the test env aiohttp is MagicMock-stubbed and the
        # constructor may run before stubs settle. _timeout() builds it per
        # call from settings (spec §18: per-page timeout = ocr_timeout).
        self._timeout_obj = None

    def _timeout(self):
        if self._timeout_obj is None:
            self._timeout_obj = aiohttp.ClientTimeout(total=self.settings.ocr_timeout)
        return self._timeout_obj

    async def transcribe(
        self, session: aiohttp.ClientSession, image_bytes: bytes, *, dpi: int
    ) -> str:
        async with self._sem:
            form = aiohttp.FormData()
            form.add_field(
                "file", image_bytes, filename="page.png", content_type="image/png"
            )
            form.add_field("dpi", str(dpi))
            async with session.post(
                f"{self.settings.ocr_url}/transcribe",
                data=form,
                timeout=self._timeout(),
            ) as resp:
                if resp.status == 503:
                    raise VisionSaturatedError("vision-ocr saturated")
                if resp.status != 200:
                    body = ""
                    text_getter = getattr(resp, "text", None)
                    if text_getter is not None:
                        try:
                            body = text_getter()
                        except Exception:  # body read is best-effort
                            body = ""
                    if asyncio.iscoroutine(body):
                        body = await body
                    raise VisionHTTPError(f"vision-ocr HTTP {resp.status}: {body}")
                data = await resp.json()
                return data.get("extracted_text") or ""
