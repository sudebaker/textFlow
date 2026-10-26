"""Contract tests for the OCR vs description split (image-ocr-description)."""

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

IMAGE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(IMAGE_DIR))

import app.main as analyzer  # noqa: E402


class _FakeRedis:
    def __init__(self):
        self.store = {}

    def get(self, key):
        return self.store.get(key)

    def setex(self, key, ttl, value):
        self.store[key] = value


@pytest.fixture(autouse=True)
def fake_redis(monkeypatch):
    fake = _FakeRedis()
    monkeypatch.setattr(analyzer, "_redis", fake)
    return fake


def _install_backends(monkeypatch, record):
    monkeypatch.setattr(analyzer, "OCR_BASE_URL", "http://ocr:8000")
    monkeypatch.setattr(analyzer, "OCR_MODEL", "ocr-model")
    monkeypatch.setattr(analyzer, "IMAGE_BASE_URL", "http://img:9000")
    monkeypatch.setattr(analyzer, "IMAGE_MODEL", "img-model")

    def _call(image_bytes, mime_type, prompt, base_url, model):
        record["base_url"] = base_url
        record["model"] = model
        record["calls"] = record.get("calls", 0) + 1
        return "A cat on a sofa"

    monkeypatch.setattr(analyzer, "_call_llm", _call)


def test_describe_uses_image_backend(monkeypatch):
    record = {}
    _install_backends(monkeypatch, record)
    client = TestClient(analyzer.app)

    response = client.post(
        "/describe", files={"file": ("cat.png", b"fake-image", "image/png")}
    )

    assert response.status_code == 200
    assert response.json()["description"] == "A cat on a sofa"
    assert record["base_url"] == "http://img:9000"
    assert record["model"] == "img-model"


def test_analyze_uses_ocr_backend_and_stays_ocr_only(monkeypatch):
    record = {}
    _install_backends(monkeypatch, record)
    client = TestClient(analyzer.app)

    response = client.post(
        "/analyze", files={"file": ("doc.png", b"other-image", "image/png")}
    )

    body = response.json()
    assert response.status_code == 200
    assert body["extracted_text"] == "A cat on a sofa"
    assert body["description"] is None
    assert record["base_url"] == "http://ocr:8000"
    assert record["model"] == "ocr-model"


def test_describe_is_cached(monkeypatch):
    record = {}
    _install_backends(monkeypatch, record)
    client = TestClient(analyzer.app)

    for _ in range(2):
        response = client.post(
            "/describe", files={"file": ("cat.png", b"same-image", "image/png")}
        )
        assert response.status_code == 200

    assert record["calls"] == 1
