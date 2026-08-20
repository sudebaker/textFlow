"""Artifact store for textFlow Python workers.

Moves large blobs (text, chunks, embeddings, results) out of Redis into a
local filesystem with content-addressed hash sharding. The abstract
ArtifactStore interface allows a future S3Store without changing callers.
"""

import hashlib
import os
import tempfile
from abc import ABC, abstractmethod
from typing import Optional, Union

ARTIFACT_REF_PREFIX = "sha256:"
DEFAULT_ARTIFACT_PATH = "/app/data/artifacts"


class ArtifactStore(ABC):
    """Content-addressed blob store.

    put() returns a reference string; get() retrieves bytes by reference.
    """

    @abstractmethod
    def put(self, data: bytes) -> str:
        """Store bytes and return a content-addressed ref (sha256:<hex>)."""

    @abstractmethod
    def get(self, ref: str) -> Optional[bytes]:
        """Retrieve bytes by ref; return None if the artifact is missing."""


class FSStore(ArtifactStore):
    """Filesystem store with hash sharding: {root}/{ab}/{cd}/{hash}.bin."""

    def __init__(self, root: str):
        self.root = root

    def _path_for(self, digest: str) -> str:
        return os.path.join(self.root, digest[:2], digest[2:4], f"{digest}.bin")

    def put(self, data: bytes) -> str:
        digest = hashlib.sha256(data).hexdigest()
        path = self._path_for(digest)
        if not os.path.exists(path):
            os.makedirs(os.path.dirname(path), exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path))
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            os.replace(tmp, path)
        return f"{ARTIFACT_REF_PREFIX}{digest}"

    def get(self, ref: str) -> Optional[bytes]:
        if not ref.startswith(ARTIFACT_REF_PREFIX):
            return None
        digest = ref[len(ARTIFACT_REF_PREFIX):]
        path = self._path_for(digest)
        try:
            with open(path, "rb") as f:
                return f.read()
        except FileNotFoundError:
            return None


def get_store() -> ArtifactStore:
    """Return the configured ArtifactStore from the ARTIFACT_PATH env var."""
    return FSStore(os.getenv("ARTIFACT_PATH", DEFAULT_ARTIFACT_PATH))


STORE = get_store()


def is_artifact_ref(value: Union[str, bytes, None]) -> bool:
    """Return True if value is an artifact reference (sha256:<hex>)."""
    if isinstance(value, bytes):
        return value.startswith(ARTIFACT_REF_PREFIX.encode())
    return isinstance(value, str) and value.startswith(ARTIFACT_REF_PREFIX)


def resolve(store: ArtifactStore, value: Union[str, bytes, None]) -> Optional[bytes]:
    """Resolve a Redis value that may be an artifact ref.

    If value is an artifact ref, return the stored bytes. Otherwise return
    value unchanged (legacy raw payload) so old data keeps working.
    """
    if is_artifact_ref(value):
        ref = value.decode() if isinstance(value, bytes) else value
        return store.get(ref)
    if isinstance(value, bytes):
        return value
    return value


def resolve_text(store: ArtifactStore, value: Union[str, bytes, None]) -> Optional[str]:
    """Resolve a Redis value that may be an artifact ref into text.

    Returns None when value is None; decodes stored bytes as utf-8 with
    errors="replace"; returns str values unchanged (legacy payloads).
    """
    data = resolve(store, value)
    if data is None:
        return None
    if isinstance(data, bytes):
        return data.decode("utf-8", errors="replace")
    return data
