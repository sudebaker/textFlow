"""Assemblers de layout staging (RED→GREEN con sandbox tmp).

gliner flat dir: pesos urchade + assets vendidos.
deberta flat dir: ficheros del snapshot + special_tokens_map vendido.
whisper seed: hub dir + shards del CAS (symlinks deben resolver a ficheros).
"""
import os
from pathlib import Path  # noqa: E402

import pytest  # noqa: E402

from download_models import assemble_deberta, assemble_gliner, seed_whisper  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]          # deploy/
ASSETS = ROOT / "models-assets"


def make_fake_cache(tmp: Path) -> Path:
    """Sandbox con snapshots falsos (gliner/deberta) y blob CAS para whisper."""
    cache = tmp / "huggingface_cache" / "hub"

    gl = cache / "models--urchade--gliner_small-v2.1" / "snapshots" / "aaaa"
    gl.mkdir(parents=True)
    (gl / "gliner_config.json").write_text("{}")
    (gl / "pytorch_model.bin").write_bytes(b"W" * 128)

    db = cache / "models--microsoft--deberta-v3-small" / "snapshots" / "bbbb"
    db.mkdir(parents=True)
    for f in ("config.json", "spm.model", "tokenizer_config.json"):
        (db / f).write_text("x")
    (db / "pytorch_model.bin").write_bytes(b"P" * 128)

    wh = cache / "models--Systran--faster-whisper-large-v2"
    snap = wh / "snapshots" / "cccc"
    snap.mkdir(parents=True)
    shard = cache / "blobs" / "98"          # CAS compartido a nivel hub (real)
    shard.mkdir(parents=True)
    blob = shard / "deadbeef"
    blob.write_bytes(b"M" * 256)
    (wh / "blobs").mkdir()
    os.symlink("../../blobs/98/deadbeef", wh / "blobs" / "bf2a97")
    for f in ("config.json", "model.bin", "tokenizer.json", "vocabulary.txt"):
        os.symlink("../../blobs/bf2a97", snap / f)
    return cache


@pytest.fixture()
def sandbox(tmp_path, monkeypatch):
    monkeypatch.setattr("download_models.STAGING_DIR", tmp_path)
    monkeypatch.setattr("download_models.HF_CACHE_DIR", tmp_path / "huggingface_cache")
    make_fake_cache(tmp_path)
    return tmp_path


def test_gliner_flat_dir_from_weights_and_vendored_assets(sandbox):
    (sandbox / "gliner-small-v2.1").mkdir(parents=True)
    assemble_gliner()
    flat = sandbox / "gliner-small-v2.1"
    for name in ("config.json", "gliner_config.json", "tokenizer.json",
                 "tokenizer_config.json", "special_tokens_map.json",
                 "spm.model", "added_tokens.json", "pytorch_model.bin"):
        assert (flat / name).is_file(), name
    assert (flat / "pytorch_model.bin").read_bytes() == b"W" * 128
    vendored = (ASSETS / "gliner-small-v2.1" / "gliner_config.json").read_bytes()
    assert (flat / "gliner_config.json").read_bytes() == vendored


def test_gliner_assemble_idempotent(sandbox):
    (sandbox / "gliner-small-v2.1").mkdir(parents=True)
    assemble_gliner()
    assemble_gliner()
    assert (sandbox / "gliner-small-v2.1" / "pytorch_model.bin").is_file()


def test_gliner_missing_weights_fails_clear(sandbox):
    (sandbox / "gliner-small-v2.1").mkdir(parents=True)
    snap = sandbox / "huggingface_cache" / "hub" / "models--urchade--gliner_small-v2.1" / "snapshots" / "aaaa"
    (snap / "pytorch_model.bin").unlink()
    with pytest.raises(SystemExit) as exc:
        assemble_gliner()
    assert "make setup-models" in str(exc.value)


def test_deberta_flat_dir_with_vendored_special_tokens(sandbox):
    (sandbox / "deberta-v3-small").mkdir(parents=True)
    assemble_deberta()
    flat = sandbox / "deberta-v3-small"
    assert {p.name for p in flat.iterdir()} == {
        "config.json", "spm.model", "tokenizer_config.json",
        "pytorch_model.bin", "special_tokens_map.json",
    }


def test_whisper_seed_resolves_symlinks(sandbox):
    seed_whisper()
    hub = sandbox / "whisper" / "models--Systran--faster-whisper-large-v2"
    assert (hub / "snapshots" / "cccc" / "model.bin").is_file()
    assert (hub / "snapshots" / "cccc" / "model.bin").read_bytes() == b"M" * 256


def test_whisper_seed_idempotent(sandbox):
    seed_whisper()
    seed_whisper()
    assert (sandbox / "whisper" / "models--Systran--faster-whisper-large-v2" /
            "snapshots" / "cccc" / "model.bin").is_file()
