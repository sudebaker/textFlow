"""Contract tests for the remote embeddings client (remote-embeddings)."""

import hashlib
from unittest.mock import MagicMock, patch

import pytest

from pkg.worker_common.embeddings_client import (
    EmbeddingsClient,
    EmbeddingsServiceError,
)


class _FakeRedis:
    def __init__(self):
        self.store = {}
        self.setex_calls = []

    def get(self, key):
        return self.store.get(key)

    def setex(self, key, ttl, value):
        self.store[key] = value
        self.setex_calls.append((key, ttl, value))


def _response(vectors):
    response = MagicMock()
    response.status_code = 200
    response.raise_for_status = MagicMock()
    response.json.return_value = {
        "data": [{"embedding": vector} for vector in vectors]
    }
    return response


def _client(redis_client=None, **kwargs):
    return EmbeddingsClient(
        base_url="http://emb:8000",
        model="bge-m3",
        redis_client=redis_client or _FakeRedis(),
        max_retries=1,
        **kwargs,
    )


class TestCacheByContentHash:
    def test_identical_text_twice_uses_one_http_call(self):
        redis_client = _FakeRedis()
        client = _client(redis_client)

        with patch("requests.post", return_value=_response([[0.1] * 1024])) as post:
            first = client.embed_batch(["hello world"])
            second = client.embed_batch(["hello world"])

        assert post.call_count == 1
        assert first.vectors == second.vectors
        assert first.degraded is False
        assert second.degraded is False

    def test_cache_key_uses_model_and_text_with_7d_ttl(self):
        redis_client = _FakeRedis()
        client = _client(redis_client)

        with patch("requests.post", return_value=_response([[0.2] * 1024])):
            client.embed_batch(["hello"])

        expected_hash = hashlib.sha256(b"bge-m3:hello").hexdigest()
        assert redis_client.setex_calls == [
            (f"emb:{expected_hash}", 7 * 24 * 3600, redis_client.store[f"emb:{expected_hash}"])
        ]

    def test_request_contract(self):
        client = _client()

        with patch("requests.post", return_value=_response([[0.3] * 1024])) as post:
            client.embed_batch(["a", "b"])

        assert post.call_args.args[0] == "http://emb:8000/v1/embeddings"
        payload = post.call_args.kwargs["json"]
        assert payload == {"model": "bge-m3", "input": ["a", "b"]}


class TestDegradedAndFailure:
    def test_partial_batch_failure_keeps_successes_and_flags_degraded(self):
        client = _client(batch_size=1)
        good = _response([[0.4] * 1024])
        bad = MagicMock()
        bad.status_code = 500
        bad.raise_for_status.side_effect = Exception("server error")

        with patch("requests.post", side_effect=[good, bad]):
            result = client.embed_batch(["ok", "boom"])

        assert result.vectors[0] == [0.4] * 1024
        assert result.vectors[1] == []
        assert result.degraded is True

    def test_total_failure_raises(self):
        client = _client()

        with patch("requests.post", side_effect=Exception("connection refused")):
            with pytest.raises(EmbeddingsServiceError):
                client.embed_batch(["boom"])

    def test_batches_are_sized(self):
        client = _client(batch_size=2)
        responses = [
            _response([[0.1] * 1024, [0.2] * 1024]),
            _response([[0.3] * 1024]),
        ]

        with patch("requests.post", side_effect=responses) as post:
            result = client.embed_batch(["a", "b", "c"])

        assert post.call_count == 2
        assert len(result.vectors) == 3
        assert result.degraded is False
