"""Vendored model assets integrity (deploy/models-assets).

Los assets pequeños (config/tokenizer) se vendorizan en el repo para que
el staging no dependa de repos upstream rotos. Los pesos llegan por HF.
"""
import hashlib
from pathlib import Path

ASSETS_DIR = Path(__file__).resolve().parents[2] / "models-assets"
MANIFEST = ASSETS_DIR / "SHA256SUMS"


def _manifest_entries():
    """SHA256SUMS en formato sha256sum: `<sha256>  <ruta relativa>`."""
    entries = {}
    for ln in MANIFEST.read_text().splitlines():
        ln = ln.strip()
        if not ln or ln.startswith("#"):
            continue
        sha, rel_path = ln.split("  ", 1)
        entries[rel_path] = sha
    return entries


def test_manifest_exists():
    assert MANIFEST.is_file(), "SHA256SUMS manifiesto ausente"


def test_manifest_entries_exist_and_match_sha256():
    entries = _manifest_entries()
    assert entries, "manifiesto vacío"
    for rel_path, expected_sha in entries.items():
        f = ASSETS_DIR / rel_path
        assert f.is_file(), f"asset ausente: {rel_path}"
        digest = hashlib.sha256(f.read_bytes()).hexdigest()
        assert digest == expected_sha, f"sha mismatch en {rel_path}"


def test_gliner_assets_have_no_weights():
    for f in (ASSETS_DIR / "gliner-small-v2.1").iterdir():
        if f.name == "SHA256SUMS":
            continue
        assert f.name not in ("pytorch_model.bin", "model.safetensors"), (
            "los pesos no se vendorizan"
        )
        assert f.stat().st_size < 12 * 1024 * 1024, f"asset demasiado grande: {f.name}"


def test_gliner_assets_complete_catalog():
    names = {f.name for f in (ASSETS_DIR / "gliner-small-v2.1").iterdir()}
    required = {
        "config.json",
        "gliner_config.json",
        "tokenizer.json",
        "tokenizer_config.json",
        "special_tokens_map.json",
        "spm.model",
        "added_tokens.json",
    }
    missing = required - names
    assert not missing, f"faltan assets gliner: {missing}"


def test_deberta_special_tokens_map_vendored():
    f = ASSETS_DIR / "deberta-v3-small" / "special_tokens_map.json"
    assert f.is_file() and f.stat().st_size > 10
