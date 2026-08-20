"""Unit tests for pkg.worker_common.artifact_store."""

import pytest

from pkg.worker_common.artifact_store import (
    ARTIFACT_REF_PREFIX,
    FSStore,
    is_artifact_ref,
    resolve,
    resolve_text,
)


def test_put_returns_prefixed_ref(tmp_path):
    store = FSStore(str(tmp_path))

    ref = store.put(b"hello")

    assert ref.startswith(ARTIFACT_REF_PREFIX)
    assert len(ref) == len(ARTIFACT_REF_PREFIX) + 64


def test_put_get_roundtrip(tmp_path):
    store = FSStore(str(tmp_path))

    ref = store.put(b"hello world")

    assert store.get(ref) == b"hello world"


def test_get_missing_returns_none(tmp_path):
    store = FSStore(str(tmp_path))

    assert store.get(ARTIFACT_REF_PREFIX + "0" * 64) is None


def test_get_non_ref_returns_none(tmp_path):
    store = FSStore(str(tmp_path))

    assert store.get("plain-value") is None


def test_sharding_layout(tmp_path):
    store = FSStore(str(tmp_path))
    ref = store.put(b"x" * 1000)
    digest = ref[len(ARTIFACT_REF_PREFIX):]

    assert (tmp_path / digest[:2] / digest[2:4] / f"{digest}.bin").exists()


def test_content_addressed_deduplicates(tmp_path):
    store = FSStore(str(tmp_path))

    assert store.put(b"same") == store.put(b"same")


def test_put_idempotent(tmp_path):
    store = FSStore(str(tmp_path))
    store.put(b"data")
    store.put(b"data")

    files = list(tmp_path.rglob("*.bin"))
    assert len(files) == 1


def test_is_artifact_ref():
    assert is_artifact_ref(ARTIFACT_REF_PREFIX + "0" * 64) is True
    assert is_artifact_ref("plain text") is False
    assert is_artifact_ref(None) is False
    assert is_artifact_ref(b"sha256:" + b"0" * 64) is True
    assert is_artifact_ref("sha256:short") is False
    assert is_artifact_ref("sha256:" + "z" * 64) is False
    assert is_artifact_ref("sha256:" + "A" * 64) is False


def test_resolve_ref_and_legacy(tmp_path):
    store = FSStore(str(tmp_path))
    ref = store.put(b"payload")

    assert resolve(store, ref) == b"payload"
    assert resolve(store, "legacy") == "legacy"
    assert resolve(store, b"legacy-bytes") == b"legacy-bytes"
    assert resolve(store, None) is None


def test_resolve_text(tmp_path):
    store = FSStore(str(tmp_path))
    ref = store.put("documento de prueba".encode("utf-8"))

    assert resolve_text(store, ref) == "documento de prueba"
    assert resolve_text(store, "raw text") == "raw text"
    assert resolve_text(store, None) is None
