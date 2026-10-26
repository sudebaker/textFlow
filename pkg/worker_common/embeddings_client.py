"""Remote embeddings client (OpenAI-compatible ``POST /v1/embeddings``).

Replaces local bge-m3 loading (remote-embeddings decision): the backend is
configured with ``EMBEDDINGS_BASE_URL``/``EMBEDDINGS_MODEL`` and there is no
local fallback. Vectors are cached in Redis by ``sha256(model+text)`` under
``emb:{hash}`` with a 7-day TTL; identical text never costs a second HTTP call.

Failure policy:
- total failure (no vector from cache or HTTP) -> ``EmbeddingsServiceError``
  so the job fails instead of silently producing empty vectors;
- partial failure -> successful vectors are returned, failed positions are
  empty lists, and ``result.degraded`` is ``True``.
"""

import hashlib
import json
import os
import time
from dataclasses import dataclass
from typing import Any, List, Optional

import requests

DEFAULT_DIMENSION = 1024
DEFAULT_CACHE_TTL_SECONDS = 7 * 24 * 3600


class EmbeddingsServiceError(RuntimeError):
    """Raised when the embeddings backend cannot satisfy a request."""


@dataclass
class EmbeddingsResult:
    """Vectors aligned with the input texts plus a partial-failure flag."""

    vectors: List[List[float]]
    degraded: bool = False


class EmbeddingsClient:
    """HTTP client with Redis content-hash cache and batched requests."""

    def __init__(
        self,
        base_url: Optional[str] = None,
        model: Optional[str] = None,
        redis_client: Any = None,
        timeout: Optional[int] = None,
        max_retries: Optional[int] = None,
        cache_ttl: Optional[int] = None,
        batch_size: Optional[int] = None,
        dimension: Optional[int] = None,
    ):
        self.base_url = (base_url or os.getenv("EMBEDDINGS_BASE_URL", "")).rstrip("/")
        if not self.base_url:
            raise EmbeddingsServiceError(
                "EMBEDDINGS_BASE_URL is required (no local embeddings fallback)"
            )

        self.model = model or os.getenv("EMBEDDINGS_MODEL", "BAAI/bge-m3")
        self.redis_client = redis_client
        self.timeout = (
            int(os.getenv("EMBEDDINGS_TIMEOUT", "30"))
            if timeout is None
            else timeout
        )
        self.max_retries = (
            int(os.getenv("EMBEDDINGS_MAX_RETRIES", "3"))
            if max_retries is None
            else max_retries
        )
        self.cache_ttl = (
            int(
                os.getenv(
                    "EMBEDDINGS_CACHE_TTL_SECONDS", str(DEFAULT_CACHE_TTL_SECONDS)
                )
            )
            if cache_ttl is None
            else cache_ttl
        )
        self.batch_size = (
            int(os.getenv("EMBEDDINGS_BATCH_SIZE", "32"))
            if batch_size is None
            else batch_size
        )
        self.dimension = (
            int(os.getenv("EMBEDDINGS_DIMENSION", str(DEFAULT_DIMENSION)))
            if dimension is None
            else dimension
        )

    def embed(self, texts: List[str], batch_size: Optional[int] = None) -> EmbeddingsResult:
        """Embed a list of texts (alias for :meth:`embed_batch`)."""
        return self.embed_batch(texts, batch_size=batch_size)

    def embed_batch(
        self, texts: List[str], batch_size: Optional[int] = None
    ) -> EmbeddingsResult:
        """Embed texts using the cache, then HTTP in batches.

        Args:
            texts: Texts to embed, in order.
            batch_size: Optional batch size override.

        Returns:
            EmbeddingsResult with one vector per input text (``[]`` for a
            vector that could not be produced) and a degradation flag.

        Raises:
            EmbeddingsServiceError: when no vector could be obtained at all.
        """
        size = batch_size or self.batch_size
        vectors: List[Optional[List[float]]] = [None] * len(texts)

        misses = []
        for index, text in enumerate(texts):
            cached = self._cache_get(text)
            if cached is not None:
                vectors[index] = cached
            else:
                misses.append((index, text))

        degraded = False
        for start in range(0, len(misses), size):
            batch = misses[start : start + size]
            try:
                batch_vectors = self._request([text for _, text in batch])
            except EmbeddingsServiceError:
                degraded = True
                continue

            for (index, text), vector in zip(batch, batch_vectors):
                vectors[index] = vector
                self._cache_set(text, vector)

        if misses and all(vector is None for vector in vectors):
            raise EmbeddingsServiceError(
                "embeddings backend returned no vectors (no local fallback)"
            )

        if any(vector is None for vector in vectors):
            degraded = True

        return EmbeddingsResult(
            vectors=[vector if vector is not None else [] for vector in vectors],
            degraded=degraded,
        )

    def _request(self, texts: List[str]) -> List[List[float]]:
        """POST one batch, retrying transient failures with backoff."""
        last_error: Optional[Exception] = None
        payload = {"model": self.model, "input": texts}

        for attempt in range(self.max_retries):
            try:
                response = requests.post(
                    f"{self.base_url}/v1/embeddings",
                    json=payload,
                    timeout=self.timeout,
                )
                response.raise_for_status()
                data = response.json().get("data") or []
                vectors = [item.get("embedding") for item in data]

                if len(vectors) != len(texts):
                    raise EmbeddingsServiceError(
                        f"expected {len(texts)} embeddings, got {len(vectors)}"
                    )
                for vector in vectors:
                    if not isinstance(vector, list) or len(vector) != self.dimension:
                        raise EmbeddingsServiceError(
                            f"unexpected embedding dimension (expected {self.dimension})"
                        )
                return vectors
            except Exception as e:
                last_error = e
                if attempt < self.max_retries - 1:
                    time.sleep(min(2**attempt, 30))

        raise EmbeddingsServiceError(
            f"embeddings request failed after {self.max_retries} attempts: {last_error}"
        )

    def _cache_key(self, text: str) -> str:
        material = f"{self.model}:{text}".encode("utf-8")
        return f"emb:{hashlib.sha256(material).hexdigest()}"

    def _cache_get(self, text: str) -> Optional[List[float]]:
        if self.redis_client is None:
            return None
        raw = self.redis_client.get(self._cache_key(text))
        if not raw:
            return None
        try:
            return json.loads(raw)
        except (TypeError, ValueError):
            return None

    def _cache_set(self, text: str, vector: List[float]) -> None:
        if self.redis_client is None:
            return
        try:
            self.redis_client.setex(
                self._cache_key(text), self.cache_ttl, json.dumps(vector)
            )
        except Exception:
            # Cache is an optimization; a Redis failure must not fail the job.
            pass
