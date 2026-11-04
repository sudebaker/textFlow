"""Selección y catálogo de download_models.py (RED→GREEN).

El catálogo solo contiene modelos de fichero local. Los que se resuelven
por endpoint (embeddings, LLM, vision) NO son descargables aquí.
"""
import pytest  # noqa: E402

from download_models import (  # noqa: E402
    CATALOG,
    ENDPOINT_RESOLVED,
    MODEL_DEPENDENCIES,
    expand_selection,
)


def test_catalog_only_local_file_models():
    assert set(CATALOG) == {"gliner", "whisper", "docling"}


def test_endpoint_models_never_in_catalog():
    endpoint_names = {"bge-m3", "minicpm-v-4.5", "qwen2.5:7b-instruct", "embeddings"}
    for name in endpoint_names & set(CATALOG):
        pytest.fail(f"modelo endpoint-resolved en catálogo: {name}")


def test_endpoint_list_documents_exclusions():
    keys = " ".join(ENDPOINT_RESOLVED.keys())
    assert "embeddings" in keys
    assert "inferencia" in keys
    assert "vision" in keys or "OCR" in keys


def test_gliner_requires_deberta_dependency():
    assert MODEL_DEPENDENCIES["gliner"] == {"gliner_deberta_tokenizer"}


def test_expand_single():
    assert expand_selection(["gliner"]) == ["gliner", "gliner_deberta_tokenizer"]


def test_expand_comma_list_and_dedup():
    got = expand_selection(["gliner,whisper", "docling", "gliner"])
    assert got == ["gliner", "gliner_deberta_tokenizer", "whisper", "docling"]


def test_expand_unknown_lists_valid_options():
    with pytest.raises(SystemExit) as exc:
        expand_selection(["flor"])
    assert "docling" in str(exc.value)
    assert "gliner" in str(exc.value)
    assert "whisper" in str(exc.value)


def test_expand_all_is_full_catalog_with_deps():
    got = expand_selection(["all"])
    assert got == ["gliner", "gliner_deberta_tokenizer", "whisper", "docling"]


def test_expand_empty_selection_is_error():
    with pytest.raises(SystemExit):
        expand_selection([])
