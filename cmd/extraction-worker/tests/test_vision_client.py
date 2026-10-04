"""Tests for vision.client — HTTP client for vision-ocr (Task E.1).

aiohttp is MagicMock-stubbed (repo pattern, see test_vision_fallback top): the
client code must work against stubs, so the timeout object is built lazily in
transcribe() via _timeout(). We fake a session whose post() returns an async
context manager yielding a fake response object.
"""

import asyncio
import os
import sys
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../.."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

for _mod in (
    "aio_pika",
    "aio_pika.abc",
    "aiohttp",
    "langdetect",
    "magic",
    "redis",
    "textstat",
    "tiktoken",
    "pypdfium2",
    "PIL",
    "PIL.Image",
):
    sys.modules.setdefault(_mod, MagicMock())

import pytest  # noqa: E402

from vision.client import VisionHTTPError, VisionOCRClient, VisionSaturatedError  # noqa: E402

from fixtures import build_pdf  # noqa: E402,F401  (ensures fixtures importable)


def make_settings(**overrides):
    from vision.config import VisionSettings

    defaults = dict(
        ocr_enabled=True,
        gate_enabled=True,
        ocr_url="http://vision-ocr:8080",
        ocr_timeout=120.0,
        max_pages_per_document=50,
        max_seconds_per_document=900.0,
        max_concurrency=4,
        image_dpi=200,
        min_chars_per_page=100,
        image_placeholder_ratio_max=0.3,
        garbage_ratio_max=0.2,
    )
    defaults.update(overrides)
    return VisionSettings(**defaults)


class FakeResponse:
    def __init__(self, status=200, payload=None, text=""):
        self.status = status
        self.headers = {"content-type": "application/json"}
        self._payload = payload if payload is not None else {}
        self._text = text

    async def json(self):
        return self._payload

    async def text(self):
        return self._text


class FakeSession:
    """session.post(...) -> async CM yielding the fake response."""

    def __init__(self, response):
        self.response = response
        self.calls = []
        self.delay = 0.0

    def post(self, url, data=None, timeout=None):
        self.calls.append({"url": url, "data": data, "timeout": timeout})
        response = self.response
        delay = self.delay

        class _CM:
            async def __aenter__(self):
                if delay:
                    await asyncio.sleep(delay)
                return response

            async def __aexit__(self, *exc):
                return False

        return _CM()


def run(client, session, image=b"\x89PNG-fake", dpi=200):
    return asyncio.run(client.transcribe(session, image, dpi=dpi))


class TestTranscribeHappyPath:
    def test_200_returns_extracted_text(self):
        session = FakeSession(FakeResponse(200, {"extracted_text": "hola mundo"}))
        client = VisionOCRClient(make_settings())
        assert run(client, session) == "hola mundo"

    def test_empty_extracted_text_returns_empty_string(self):
        session = FakeSession(FakeResponse(200, {"extracted_text": ""}))
        client = VisionOCRClient(make_settings())
        assert run(client, session) == ""

    def test_missing_extracted_text_returns_empty_string(self):
        session = FakeSession(FakeResponse(200, {}))
        client = VisionOCRClient(make_settings())
        assert run(client, session) == ""

    def test_posts_to_transcribe_endpoint_with_form(self):
        session = FakeSession(FakeResponse(200, {"extracted_text": "x"}))
        client = VisionOCRClient(make_settings(ocr_url="http://vocr:9999"))
        run(client, session, dpi=150)
        assert session.calls[0]["url"] == "http://vocr:9999/transcribe"
        form = session.calls[0]["data"]
        # FormData is a MagicMock under stubs — the client must have registered
        # the file field (image bytes) and the dpi field.
        form.add_field.assert_any_call(
            "file", b"\x89PNG-fake", filename="page.png", content_type="image/png"
        )
        form.add_field.assert_any_call("dpi", "150")


class TestErrorMapping:
    def test_503_raises_vision_saturated_error(self):
        session = FakeSession(FakeResponse(503, {}, text="Saturated"))
        client = VisionOCRClient(make_settings())
        with pytest.raises(VisionSaturatedError):
            run(client, session)

    def test_500_raises_vision_http_error_with_body(self):
        session = FakeSession(FakeResponse(500, {}, text="boom detail"))
        client = VisionOCRClient(make_settings())
        with pytest.raises(VisionHTTPError) as excinfo:
            run(client, session)
        assert "500" in str(excinfo.value)
        assert "boom detail" in str(excinfo.value)

    def test_404_raises_vision_http_error(self):
        session = FakeSession(FakeResponse(404, {}, text="nope"))
        client = VisionOCRClient(make_settings())
        with pytest.raises(VisionHTTPError):
            run(client, session)


class TestClientSideSemaphore:
    def _run_two(self, settings, delay=0.01):
        """Run two concurrent transcribes against a tracking session.

        Returns (results, peak_in_flight, events). peak is observed inside
        post() AND inside __aenter__ (after the semaphore gate, before the
        response): with max_concurrency=1 both observables must cap at 1.
        """
        events = []

        class TrackingSession:
            def __init__(self):
                self.active = 0
                self.peak = 0
                self.gate_peak = 0

            def post(self, url, data=None, timeout=None):
                outer = self
                outer.active += 1
                outer.peak = max(outer.peak, outer.active)
                events.append("post")

                class _CM:
                    async def __aenter__(self):
                        # Observe again INSIDE the request coroutine: past the
                        # client semaphore, at the moment the HTTP call starts.
                        outer.gate_peak = max(outer.gate_peak, outer.active)
                        await asyncio.sleep(delay)
                        events.append("resp")
                        return FakeResponse(200, {"extracted_text": "t"})

                    async def __aexit__(self, *exc):
                        events.append("exit")
                        outer.active -= 1
                        return False

                return _CM()

        session = TrackingSession()
        client = VisionOCRClient(settings)

        async def two_calls():
            return await asyncio.gather(
                client.transcribe(session, b"img1", dpi=200),
                client.transcribe(session, b"img2", dpi=200),
            )

        results = asyncio.run(two_calls())
        return results, session.peak, session.gate_peak, events

    def test_max_concurrency_one_serializes_calls(self):
        results, post_peak, gate_peak, events = self._run_two(
            make_settings(max_concurrency=1)
        )
        assert results == ["t", "t"]
        # Serialized: never two requests in flight at once (observed at post
        # registration and inside the request coroutine).
        assert post_peak <= 1, f"interleaved at post-obs level: peak={post_peak}"
        assert gate_peak <= 1, f"interleaved at gate level: peak={gate_peak}"
        # Exact serialized order: resp|exit|resp|exit — the second request only
        # starts after the first released the semaphore.
        assert events == ["post", "resp", "exit", "post", "resp", "exit"]

    def test_semaphore_permits_configured_concurrency(self):
        # max_concurrency=2: two calls DO overlap (peak in-flight == 2) —
        # proves the gate exists and is calibrated, not accidentally serial.
        _, post_peak, gate_peak, _ = self._run_two(
            make_settings(max_concurrency=2), delay=0.02
        )
        assert post_peak == 2
        assert gate_peak == 2
