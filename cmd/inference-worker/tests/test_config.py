"""Contract tests for the inference-worker env separation (D6).

The inference worker must read `INFERENCE_BASE_URL`/`INFERENCE_MODEL` only.
`LLM_URL`/`LLM_MODEL` are not a fallback source, and an incomplete
configuration fails at startup instead of silently disabling inferences.
"""

import pytest

import inference_worker


class TestInferenceEnvContract:
    def test_requires_base_url_and_model(self):
        with pytest.raises(RuntimeError) as excinfo:
            inference_worker.resolve_inference_config({})

        message = str(excinfo.value)
        assert "INFERENCE_BASE_URL" in message
        assert "INFERENCE_MODEL" in message

    def test_legacy_llm_env_is_not_a_fallback(self):
        with pytest.raises(RuntimeError):
            inference_worker.resolve_inference_config(
                {
                    "LLM_URL": "http://legacy:8000",
                    "LLM_MODEL": "legacy-model",
                }
            )

    def test_single_missing_variable_still_fails(self):
        with pytest.raises(RuntimeError):
            inference_worker.resolve_inference_config(
                {"INFERENCE_BASE_URL": "http://vllm:8000"}
            )
        with pytest.raises(RuntimeError):
            inference_worker.resolve_inference_config(
                {"INFERENCE_MODEL": "qwen3-coder"}
            )

    def test_blank_values_count_as_unset(self):
        with pytest.raises(RuntimeError):
            inference_worker.resolve_inference_config(
                {"INFERENCE_BASE_URL": "   ", "INFERENCE_MODEL": "  "}
            )

    def test_returns_normalized_new_env_values(self):
        base_url, model = inference_worker.resolve_inference_config(
            {
                "INFERENCE_BASE_URL": "http://vllm:8000/",
                "INFERENCE_MODEL": "qwen3-coder",
            }
        )

        assert base_url == "http://vllm:8000"
        assert model == "qwen3-coder"

    def test_legacy_constants_are_gone(self):
        assert not hasattr(inference_worker, "LLM_URL")
        assert not hasattr(inference_worker, "LLM_MODEL")

    def test_new_constants_exist(self):
        assert hasattr(inference_worker, "INFERENCE_BASE_URL")
        assert hasattr(inference_worker, "INFERENCE_MODEL")


class TestStartupWiring:
    def test_main_fails_fast_without_env(self, monkeypatch):
        monkeypatch.delenv("INFERENCE_BASE_URL", raising=False)
        monkeypatch.delenv("INFERENCE_MODEL", raising=False)

        def _no_worker():
            raise AssertionError("InferenceWorker constructed before env validation")

        monkeypatch.setattr(inference_worker, "InferenceWorker", _no_worker)
        with pytest.raises(RuntimeError):
            inference_worker.main()

    def test_main_ignores_legacy_env(self, monkeypatch):
        monkeypatch.delenv("INFERENCE_BASE_URL", raising=False)
        monkeypatch.delenv("INFERENCE_MODEL", raising=False)
        monkeypatch.setenv("LLM_URL", "http://legacy:8000")
        monkeypatch.setenv("LLM_MODEL", "legacy-model")

        def _no_worker():
            raise AssertionError("InferenceWorker constructed before env validation")

        monkeypatch.setattr(inference_worker, "InferenceWorker", _no_worker)
        with pytest.raises(RuntimeError):
            inference_worker.main()
