"""Checks puros de doctor.py (RED→GREEN).

Red/backends se validan inyectando el fetcher; staging con sandbox tmp.
"""
from pathlib import Path  # noqa: E402

import pytest  # noqa: E402

from doctor import (  # noqa: E402
    REQUIRED_BACKEND_VARS,
    parse_env,
    check_required_vars,
    check_url_sanity,
    check_staging,
)


def test_required_vars_list_backends():
    assert set(REQUIRED_BACKEND_VARS) == {
        "EMBEDDINGS_BASE_URL", "EMBEDDINGS_MODEL", "EMBEDDINGS_DIMENSION",
        "INFERENCE_BASE_URL", "INFERENCE_MODEL",
    }


def test_parse_env_skips_comments_and_quotes():
    env = parse_env('A=1\n# comentario\nB="hola mundo"\n  C = 3 \n\nD=h')
    assert env == {"A": "1", "B": "hola mundo", "C": "3", "D": "h"}


def test_parse_env_supports_export_prefix():
    env = parse_env('export EMBEDDINGS_DIMENSION=768\nexport X=1 # nota')
    assert env.get("EMBEDDINGS_DIMENSION") == "768"
    assert env.get("X") == "1"


def test_parse_env_strips_inline_comments():
    env = parse_env('EMBEDDINGS_DIMENSION=768  # dims del backend\nA=v # x')
    assert env.get("EMBEDDINGS_DIMENSION") == "768"
    assert env.get("A") == "v"


def test_check_required_vars_fail_fast_names_missing():
    ok, missing = check_required_vars({"EMBEDDINGS_BASE_URL": "http://x"})
    assert not ok and set(missing) == {
        "EMBEDDINGS_MODEL", "EMBEDDINGS_DIMENSION",
        "INFERENCE_BASE_URL", "INFERENCE_MODEL",
    }


def test_check_required_vars_empty_string_counts_as_missing():
    ok, missing = check_required_vars({v: "" for v in REQUIRED_BACKEND_VARS})
    assert not ok and len(missing) == 5


def test_check_url_sanity_rejects_v1_suffix():
    ok, msg = check_url_sanity({"EMBEDDINGS_BASE_URL": "http://h:11434/v1"})
    assert not ok and "/v1" in msg


def test_check_url_sanity_accepts_plain_base():
    ok, msg = check_url_sanity({
        "EMBEDDINGS_BASE_URL": "http://host.docker.internal:11434",
        "INFERENCE_BASE_URL": "http://vllm:8000",
    })
    assert ok


def test_check_url_sanity_urlless_scheme_no_crash():
    ok, msg = check_url_sanity({"EMBEDDINGS_BASE_URL": "localhost:11434"})
    assert ok  # sin sufijo /v1 — válido


def test_staging_requires_gliner_and_deberta(tmp_path):
    (tmp_path / "gliner-small-v2.1").mkdir()
    problems = check_staging(tmp_path)
    assert any("gliner" in p for p in problems)
    assert any("deberta" in p for p in problems)


def test_staging_ok_when_local_models_complete(tmp_path):
    gl = tmp_path / "gliner-small-v2.1"
    gl.mkdir()
    for f in ("config.json", "gliner_config.json", "tokenizer.json",
              "tokenizer_config.json", "special_tokens_map.json",
              "spm.model", "added_tokens.json", "pytorch_model.bin"):
        (gl / f).write_text("x")
    db = tmp_path / "deberta-v3-small"
    db.mkdir()
    for f in ("config.json", "spm.model", "tokenizer_config.json",
              "pytorch_model.bin", "special_tokens_map.json"):
        (db / f).write_text("x")
    problems = check_staging(tmp_path)
    assert "gliner-small-v2.1 incompleto" not in " ".join(problems)
    assert "deberta-v3-small incompleto" not in " ".join(problems)


def test_staging_warns_for_whisper_and_docling(tmp_path):
    gl = tmp_path / "gliner-small-v2.1"; gl.mkdir()
    (gl / "pytorch_model.bin").write_text("x")
    db = tmp_path / "deberta-v3-small"; db.mkdir()
    (db / "config.json").write_text("x")
    problems = check_staging(tmp_path)
    joined = " ".join(problems)
    assert "whisper" in joined and "docling" in joined
