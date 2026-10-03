"""Fakes for the extraction-worker golden regression test (Task 0.2).

These stand in for the real I/O boundaries (Redis, FSStore, aio_pika
channel/exchange, aio_pika.Message) so ``_process_message_async`` can run
end-to-end offline while every side effect is captured for later
canonicalization and golden comparison.
"""

import hashlib
import json


class FakeRedis:
    """Records every write in order as tuples; hget always returns None.

    The worker only needs hset/set/publish/hget from Redis during extraction
    (job never appears cancelled since hget -> None).
    """

    def __init__(self):
        self.writes = []

    def hset(self, key, *args, **kw):
        self.writes.append(("hset", key, args, kw))

    def set(self, key, value):
        self.writes.append(("set", key, value))

    def hget(self, key, field):
        return None

    def publish(self, channel, msg):
        self.writes.append(("publish", channel, msg))


class FakeStore:
    """In-memory ArtifactStore mirroring FSStore ref semantics.

    ``put(data)`` returns ``"sha256:" + hexdigest`` exactly like
    ``pkg/worker_common/artifact_store.py:FSStore.put``.
    """

    def __init__(self):
        self.blobs = {}

    def put(self, data):
        digest = hashlib.sha256(data).hexdigest()
        self.blobs.setdefault(digest, data)
        return f"sha256:{digest}"

    def get(self, ref):
        return self.blobs.get(ref[len("sha256:"):]) if isinstance(ref, str) and ref.startswith("sha256:") else None


class FakeExchange:
    """Stands in for an aio_pika exchange; records every publish call."""

    def __init__(self):
        self.published = []

    async def publish(self, message, routing_key=None, **kw):
        kwargs = sorted(getattr(message, "kwargs", {}).items())
        self.published.append((routing_key, message.body, kwargs))


class FakeMsg:
    """Replaces ``aio_pika.Message`` so captured bodies are the real bytes.

    Message contract used by worker.py: ``aio_pika.Message(body=..., **kw)``
    with ``.body`` and ``.kwargs`` exposing the construction kwargs.
    """

    def __init__(self, body=None, **kwargs):
        self.body = body
        self.kwargs = kwargs


def normalize(writes, published) -> dict:
    """Canonicalize captured side effects for deterministic golden comparison.

    Rules (plan Task 0.2):
    - bytes -> utf-8 decode
    - non-JSON-serializable values -> "<repr>"
    - values in timestamp fields (``queued_at`` in the published job message,
      ``timestamp`` in EventBus pub/sub payloads) -> "<ts>"
    Output is sorted-key JSON-serializable so ``json.dumps`` is deterministic.
    """
    normalized_writes = [
        {"op": w[0], "key": w[1], "value": _norm_value(w[2])} for w in writes
    ]
    normalized_published = [
        {
            "routing_key": routing_key,
            "body": _norm_value(body),
            # kwargs sorted by key already at capture time in FakeExchange.
            "kwargs": _norm_value(dict(kwargs)) if kwargs else {},
        }
        for routing_key, body, kwargs in published
    ]
    result = {
        "writes": normalized_writes,
        "published": normalized_published,
    }
    # Fail loudly if canonicalization produced anything non-serializable.
    json.dumps(result, sort_keys=True)
    return result


def canonical_json(result: dict) -> str:
    """Deterministic JSON dump of a ``normalize`` result."""
    return json.dumps(result, sort_keys=True, indent=2)


_TIMESTAMP_FIELDS = {"queued_at", "timestamp"}


def _norm_value(value):
    if isinstance(value, bytes):
        # Fall through to the str logic: published/redis payloads are bytes
        # that may BE JSON documents (job message, EventBus events).
        return _norm_value(value.decode("utf-8", errors="replace"))
    if isinstance(value, dict):
        out = {}
        for k in sorted(value, key=str):
            key = k.decode("utf-8") if isinstance(k, bytes) else k
            # Timestamps never survive into the golden: queued_at (worker),
            # timestamp (EventBus pub/sub payloads).
            out[key] = "<ts>" if key in _TIMESTAMP_FIELDS else _norm_value(value[k])
        return out
    if isinstance(value, (list, tuple)):
        return [_norm_value(v) for v in value]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        if value[:1] in ("{", "["):
            # Nested JSON payload (published job message, EventBus pub/sub
            # event): parse and recurse so inner **timestamp fields are
            # neutralized too.
            try:
                parsed = json.loads(value)
            except ValueError:
                return value
            if isinstance(parsed, (dict, list)):
                return _norm_value(parsed)
        return value
    # Non-JSON-serializable (MagicMocks, sentinels from real I/O objects).
    return "<repr>"
