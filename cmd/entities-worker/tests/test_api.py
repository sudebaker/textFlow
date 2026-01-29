import importlib
import os
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_MODEL_PATH = Path(os.getenv("GLINER_MODEL_PATH", "/models/gliner_model"))


def _reload_app_with_env(env: dict) -> TestClient:
    for key, value in env.items():
        os.environ[key] = value

    # Ensure module re-import uses the updated env
    if "main" in sys.modules:
        del sys.modules["main"]

    sys.path.insert(0, str(ROOT))
    main = importlib.import_module("main")
    return TestClient(main.app)


@pytest.fixture(scope="session")
def client(tmp_path_factory: pytest.TempPathFactory) -> TestClient:
    env: dict = {}

    if DEFAULT_MODEL_PATH.exists():
        env["GLINER_USE_MOCK"] = "false"
        env["GLINER_MODEL_PATH"] = str(DEFAULT_MODEL_PATH)
    else:
        dummy_model = tmp_path_factory.mktemp("gliner_model")
        env["GLINER_USE_MOCK"] = "true"
        env["GLINER_MODEL_PATH"] = str(dummy_model)

    return _reload_app_with_env(env)


def test_extract_success(client: TestClient):
    payload = {
        "text": "Juan Perez trabaja en Madrid",
        "options": {"entity_types": ["PER", "LOC"]},
    }

    resp = client.post("/api/v1/extract", json=payload)
    assert resp.status_code == 200
    body = resp.json()
    assert body.get("success") is True
    assert isinstance(body.get("entities"), list)


def test_extract_empty_text_returns_400(client: TestClient):
    resp = client.post("/api/v1/extract", json={"text": ""})
    assert resp.status_code == 400


def test_batch_success(client: TestClient):
    payload = {"chunks": ["Uno", "Dos"]}
    resp = client.post("/api/v1/extract/batch", json=payload)
    assert resp.status_code == 200
    body = resp.json()
    assert body.get("success") is True
    assert isinstance(body.get("results"), list)


def test_health(client: TestClient):
    resp = client.get("/api/v1/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body.get("status") in {"healthy", "degraded"}
    assert "checks" in body


def test_metrics_exposes_prometheus_format(client: TestClient):
    resp = client.get("/metrics")
    assert resp.status_code == 200
    assert "text/plain" in resp.headers.get("content-type", "")
