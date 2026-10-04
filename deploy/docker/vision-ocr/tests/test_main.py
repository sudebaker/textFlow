"""Tests for the vision-ocr FastAPI service (spec extraccion-visual §9-§13, §20).

Covers: happy path (200 + cache-SET), cache hit (single LLM call), empty file
(400), saturation (503 + Retry-After: 5), vLLM retries exhausted (502),
/health in-flight gauge. The LLM backend and Redis are mocked: requests.post
is replaced and vision._redis is a dict-backed fake. The service reads env
vars at import time, so the test env is set BEFORE `import app.main`.
"""

import asyncio
import json
import os
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Environment BEFORE importing app.main (module-level constants are captured
# on import). 'app' is a generic package name shared by several worker suites:
# evict stale entries so this suite always binds deploy/docker/vision-ocr.
# ---------------------------------------------------------------------------
_HERE = Path(__file__).resolve().parent
_SERVICE_DIR = _HERE.parent
sys.path.insert(0, str(_SERVICE_DIR))
for _name in list(sys.modules):
    if _name == "app" or _name.startswith("app."):
        sys.modules.pop(_name, None)

os.environ["REDIS_URL"] = "redis://localhost:6379/9"  # sentinel; _redis is patched per test
os.environ["VISION_LLM_BASE_URL"] = "http://mock-llm:8000"
os.environ["VISION_MODEL"] = "test-vision-model"
os.environ["VISION_TIMEOUT"] = "5"
os.environ["VISION_MAX_RETRIES"] = "2"
os.environ["VISION_MAX_CONCURRENCY"] = "1"
os.environ["VISION_QUEUE_WAIT_SECONDS"] = "1"

import app.main as vision  # noqa: E402
import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

app = vision.app
client = TestClient(app)

FAKE_PNG = b"\x89PNG-fake-content"
FAKE_FILE = ("page.png", FAKE_PNG, "image/png")


class FakeRedis:
    """Dict-backed stand-in for app.main._redis (get/setex only)."""

    def __init__(self):
        self.store = {}

    def get(self, key):
        return self.store.get(key)

    def setex(self, key, ttl, value):
        self.store[key] = value


class FakeLLMResponse:
    """OpenAI-compatible /v1/chat/completions response stub."""

    status_code = 200

    def __init__(self, content="  transcribed text  "):
        self._content = content

    def raise_for_status(self):
        pass

    def json(self):
        return {"choices": [{"message": {"content": self._content}}]}


@pytest.fixture
def fake_redis(monkeypatch):
    stub = FakeRedis()
    monkeypatch.setattr(vision, "_redis", stub)
    return stub


@pytest.fixture
def llm_calls(monkeypatch):
    """Replace vision.requests.post; returns (calls, set_response)."""
    calls = []

    def set_response(fn):
        monkeypatch.setattr(vision.requests, "post", fn)

    def record(fn):
        def wrapper(url, json=None, timeout=None):
            payload = json
            calls.append((url, payload, timeout))
            return fn(url, json=payload, timeout=timeout)

        return wrapper

    return calls, record, set_response


def post_transcribe():
    return client.post("/transcribe", files={"file": FAKE_FILE})


def test_transcribe_happy_path_returns_text_and_caches(fake_redis, llm_calls):
    calls, record, set_response = llm_calls
    posts = lambda url, json=None, timeout=None: FakeLLMResponse()
    set_response(record(posts))

    resp = post_transcribe()

    assert resp.status_code == 200
    body = resp.json()
    assert body["extracted_text"] == "transcribed text"
    assert body["cached"] is False
    assert body["model"] == "test-vision-model"
    assert body["dpi"] == 0
    assert isinstance(body["elapsed_ms"], int)
    # cache miss -> one LLM call + best-effort cache SET
    assert len(calls) == 1
    url, payload, timeout = calls[0]
    assert url == "http://mock-llm:8000/v1/chat/completions"
    assert payload["model"] == "test-vision-model"
    assert payload["temperature"] == 0
    assert payload["stream"] is False
    assert payload["messages"][0]["content"][0]["type"] == "text"
    assert "data:image/png;base64," in payload["messages"][0]["content"][1]["image_url"]["url"]
    assert timeout == vision.VISION_TIMEOUT
    assert len(fake_redis.store) == 1
    cached_key = next(iter(fake_redis.store))
    assert cached_key.startswith("vision:")
    stored = json.loads(fake_redis.store[cached_key])
    assert stored["extracted_text"] == "transcribed text"


def test_transcribe_second_call_hits_cache_without_llm(fake_redis, llm_calls):
    calls, record, set_response = llm_calls
    posts = lambda url, json=None, timeout=None: FakeLLMResponse()
    set_response(record(posts))

    first = post_transcribe()
    second = post_transcribe()

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json() == first.json()
    # one vLLM call total: the second POST is served from the cache
    assert len(calls) == 1
    assert len(fake_redis.store) == 1


def test_transcribe_empty_file_returns_400(fake_redis, llm_calls):
    calls, record, set_response = llm_calls
    posts = lambda url, json=None, timeout=None: FakeLLMResponse()
    set_response(record(posts))

    resp = client.post("/transcribe", files={"file": ("empty.png", b"", "image/png")})

    assert resp.status_code == 400
    assert len(calls) == 0


def test_transcribe_saturated_returns_503_with_retry_after(fake_redis, llm_calls):
    calls, record, set_response = llm_calls
    posts = lambda url, json=None, timeout=None: FakeLLMResponse()
    set_response(record(posts))

    async def hold():
        await vision._sem.acquire()

    try:
        asyncio.run(hold())  # MAX_CONCURRENCY=1 at import: slot is now taken
        resp = post_transcribe()

        assert resp.status_code == 503
        assert resp.json() == {"detail": "Saturated"}
        assert resp.headers["Retry-After"] == "5"
        assert len(calls) == 0
    finally:
        vision._sem.release()
        assert vision._sem._value == 1


def test_transcribe_llm_retries_exhausted_returns_502(fake_redis, llm_calls, monkeypatch):
    calls, record, set_response = llm_calls
    monkeypatch.setattr(vision.time, "sleep", lambda s: None)  # skip backoff wait

    def failing_post(url, json=None, timeout=None):
        raise vision.requests.ConnectionError("refused")

    set_response(record(failing_post))

    resp = post_transcribe()

    assert resp.status_code == 502
    assert "refused" in resp.json()["detail"]
    assert len(calls) == vision.VISION_MAX_RETRIES  # no sleep-based retry blowup: patched
    assert len(fake_redis.store) == 0  # nothing cached after a failed call


def test_health_reports_in_flight_and_concurrency():
    resp = client.get("/health")

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "healthy"
    assert body["model"] == "test-vision-model"
    assert body["backend"] == "http://mock-llm:8000"
    assert body["max_concurrency"] == 1
    assert "in_flight" in body
