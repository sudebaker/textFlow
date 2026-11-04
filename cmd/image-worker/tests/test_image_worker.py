import asyncio
import json
import os
import tempfile
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pkg.worker_common.async_base import BaseAsyncWorker


@pytest.fixture(autouse=True)
def _stub_describe():
    """Default: no description requested (tests opt in explicitly)."""
    with patch(
        "pkg.image_client.client.MultimodalLLMClientPool.describe",
        return_value=None,
    ):
        yield


@pytest.fixture
def mock_deps():
    mock_redis_client = MagicMock()
    mock_event_bus = MagicMock()
    with patch.object(BaseAsyncWorker, "redis_client", mock_redis_client), \
         patch.object(BaseAsyncWorker, "event_bus", mock_event_bus), \
         patch("pkg.worker_common.async_base.Counter"), \
         patch("pkg.worker_common.async_base.Histogram"), \
         patch("pkg.worker_common.async_base.Gauge"):
        yield {
            "redis_client": mock_redis_client,
            "event_bus": mock_event_bus,
        }


def _make_msg(
    job_id="j1",
    filename="test.png",
    entity_types=None,
    features=None,
    path=None,
    profile=None,
):
    body = {
        "job_id": job_id,
        "document_path": path or "/tmp/test.png",
        "filename": filename,
    }
    if entity_types:
        body["entity_types"] = entity_types
    if features:
        body["features"] = features
    if profile:
        body["profile"] = profile
    return body


def _routing_keys(channel):
    """Routing keys of every publish, in order (envelopes + inference chunks)."""
    return [
        call.kwargs.get("routing_key") or (call.args[0] if call.args else None)
        for call in channel.default_exchange.publish.call_args_list
    ]


# --- Basic tests ---

class TestImageWorkerInit:
    def test_has_worker_name(self, mock_deps):
        from worker import ImageWorker
        w = ImageWorker()
        assert w.worker_name == "image-worker"

    def test_has_queue_name(self, mock_deps):
        from worker import ImageWorker
        w = ImageWorker()
        assert w.queue_name == os.getenv("IMAGE_QUEUE", "image")

    def test_creates_llm_pool(self, mock_deps):
        from worker import ImageWorker
        w = ImageWorker()
        assert w.llm_pool is not None


# --- Success path ---

class TestProcessMessageSuccess:
    @pytest.mark.asyncio
    async def test_sets_status_analyzing(self, mock_deps):
        from worker import ImageWorker
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            f.write(b"\x89PNG\r\n\x1a\n")
            path = f.name
        try:
            w = ImageWorker()
            w._channel = AsyncMock()
            mock_result = MagicMock()
            mock_result.extracted_text = "Extracted text"
            mock_result.description = "A description"
            mock_result.language = "en"
            with patch.object(w.llm_pool, "analyze", return_value=mock_result):
                body = _make_msg(path=path)
                await w.process_message(body)
            mock_deps["redis_client"].hset.assert_any_call(
                "orchestrator:job:j1:status", "status", "analyzing_image"
            )
        finally:
            os.unlink(path)

    @pytest.mark.asyncio
    async def test_stores_text_in_redis(self, mock_deps, monkeypatch, tmp_path):
        import worker as image_worker
        from pkg.worker_common.artifact_store import FSStore

        store = FSStore(str(tmp_path))
        monkeypatch.setattr(image_worker, "STORE", store)
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            f.write(b"\x89PNG\r\n\x1a\n")
            path = f.name
        try:
            w = image_worker.ImageWorker()
            w._channel = AsyncMock()
            mock_result = MagicMock()
            mock_result.extracted_text = "OCR text here"
            mock_result.description = None
            mock_result.language = "es"
            with patch.object(w.llm_pool, "analyze", return_value=mock_result):
                await w.process_message(_make_msg(path=path))
            ref = store.put("OCR text here".encode("utf-8"))
            mock_deps["redis_client"].set.assert_any_call(
                "orchestrator:job:j1:text", ref
            )
        finally:
            os.unlink(path)

    @pytest.mark.asyncio
    async def test_full_profile_publishes_all_four_stages(self, mock_deps):
        from worker import ImageWorker
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            f.write(b"\x89PNG\r\n\x1a\n")
            path = f.name
        try:
            w = ImageWorker()
            w._channel = AsyncMock()
            mock_result = MagicMock()
            mock_result.extracted_text = "text"
            mock_result.description = None
            mock_result.language = "en"
            with patch.object(w.llm_pool, "analyze", return_value=mock_result):
                await w.process_message(_make_msg(path=path))
            assert {"embeddings", "entities", "metadata", "inferences"} <= set(
                _routing_keys(w._channel)
            )
        finally:
            os.unlink(path)

    @pytest.mark.asyncio
    async def test_rag_profile_skips_entities_and_metadata(self, mock_deps):
        from worker import ImageWorker
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            f.write(b"\x89PNG\r\n\x1a\n")
            path = f.name
        try:
            w = ImageWorker()
            w._channel = AsyncMock()
            mock_result = MagicMock()
            mock_result.extracted_text = "text"
            mock_result.description = None
            mock_result.language = "en"
            with patch.object(w.llm_pool, "analyze", return_value=mock_result):
                await w.process_message(_make_msg(profile="rag", path=path))
            routing = _routing_keys(w._channel)
            assert {"embeddings", "inferences"} <= set(routing)
            assert "entities" not in routing
            assert "metadata" not in routing
        finally:
            os.unlink(path)

    @pytest.mark.asyncio
    async def test_dispatches_inference_chunks_with_counters(self, mock_deps):
        """The image entry stage owns dispatch: only chunk messages go to inferences."""
        from worker import ImageWorker
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            f.write(b"\x89PNG\r\n\x1a\n")
            path = f.name
        try:
            w = ImageWorker()
            w._channel = AsyncMock()
            mock_result = MagicMock()
            mock_result.extracted_text = "text"
            mock_result.description = None
            mock_result.language = "en"
            with patch.object(w.llm_pool, "analyze", return_value=mock_result):
                await w.process_message(_make_msg(path=path))
            routing = _routing_keys(w._channel)
            assert routing.count("inferences") == 1, routing
            mock_deps["redis_client"].setex.assert_any_call(
                "orchestrator:job:j1:inferences:remaining", 86400, 1
            )
            mock_deps["redis_client"].setex.assert_any_call(
                "orchestrator:job:j1:inferences:total", 86400, 1
            )
        finally:
            os.unlink(path)

    @pytest.mark.asyncio
    async def test_steps_marked_completed(self, mock_deps):
        from worker import ImageWorker
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            f.write(b"\x89PNG\r\n\x1a\n")
            path = f.name
        try:
            w = ImageWorker()
            w._channel = AsyncMock()
            mock_result = MagicMock()
            mock_result.extracted_text = "t"
            mock_result.description = None
            mock_result.language = "en"
            with patch.object(w.llm_pool, "analyze", return_value=mock_result):
                await w.process_message(_make_msg(path=path))
            mock_deps["redis_client"].hset.assert_any_call(
                "orchestrator:job:j1:steps", "image", "completed"
            )
        finally:
            os.unlink(path)


# --- Error path ---

class TestProcessMessageError:
    @pytest.mark.asyncio
    async def test_marks_job_failed_on_llm_error(self, mock_deps):
        from worker import ImageWorker
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            f.write(b"\x89PNG\r\n\x1a\n")
            path = f.name
        try:
            w = ImageWorker()
            w._channel = AsyncMock()
            with patch.object(w.llm_pool, "analyze", side_effect=RuntimeError("LLM down")):
                with pytest.raises(RuntimeError, match="LLM down"):
                    await w.process_message(_make_msg(path=path))
            mock_deps["redis_client"].hset.assert_any_call(
                "orchestrator:job:j1:status", "status", "failed"
            )
        finally:
            os.unlink(path)

    @pytest.mark.asyncio
    async def test_sets_error_message_in_redis(self, mock_deps):
        from worker import ImageWorker
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            f.write(b"\x89PNG\r\n\x1a\n")
            path = f.name
        try:
            w = ImageWorker()
            w._channel = AsyncMock()
            with patch.object(w.llm_pool, "analyze", side_effect=RuntimeError("timeout")):
                with pytest.raises(RuntimeError):
                    await w.process_message(_make_msg(path=path))
            mock_deps["redis_client"].set.assert_any_call(
                "orchestrator:job:j1:error", "timeout"
            )
        finally:
            os.unlink(path)

    @pytest.mark.asyncio
    async def test_publishes_job_failed_event(self, mock_deps):
        from worker import ImageWorker
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            f.write(b"\x89PNG\r\n\x1a\n")
            path = f.name
        try:
            w = ImageWorker()
            w._channel = AsyncMock()
            with patch.object(w.llm_pool, "analyze", side_effect=RuntimeError("fail")):
                with pytest.raises(RuntimeError):
                    await w.process_message(_make_msg(path=path))
            mock_deps["event_bus"].publish_job_failed.assert_called_once()
        finally:
            os.unlink(path)


# --- Routing / features ---

class TestRouting:
    @pytest.mark.asyncio
    async def test_inferences_feature_publishes_4_queues(self, mock_deps):
        from worker import ImageWorker
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            f.write(b"\x89PNG\r\n\x1a\n")
            path = f.name
        try:
            w = ImageWorker()
            w._channel = AsyncMock()
            mock_result = MagicMock()
            mock_result.extracted_text = "t"
            mock_result.description = None
            mock_result.language = "en"
            with patch.object(w.llm_pool, "analyze", return_value=mock_result):
                await w.process_message(_make_msg(features=["inferences"], path=path))
            published_queues = [c.kwargs.get("routing_key") or (c.args[0] if c.args else None) for c in w._channel.default_exchange.publish.call_args_list]
            assert "inferences" in published_queues
        finally:
            os.unlink(path)

    @pytest.mark.asyncio
    async def test_no_entity_types_does_not_fail(self, mock_deps):
        from worker import ImageWorker
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            f.write(b"\x89PNG\r\n\x1a\n")
            path = f.name
        try:
            w = ImageWorker()
            w._channel = AsyncMock()
            mock_result = MagicMock()
            mock_result.extracted_text = "t"
            mock_result.description = None
            mock_result.language = "en"
            with patch.object(w.llm_pool, "analyze", return_value=mock_result):
                await w.process_message(_make_msg(path=path))
            envelope_queues = [
                q for q in _routing_keys(w._channel) if q != "inferences"
            ]
            assert envelope_queues == ["embeddings", "entities", "metadata"]
        finally:
            os.unlink(path)

    @pytest.mark.asyncio
    async def test_publish_body_has_no_inline_chunks(self, mock_deps):
        from worker import ImageWorker
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            f.write(b"\x89PNG\r\n\x1a\n")
            path = f.name
        try:
            w = ImageWorker()
            w._channel = AsyncMock()
            mock_result = MagicMock()
            mock_result.extracted_text = "text"
            mock_result.description = None
            mock_result.language = "en"
            with patch.object(w.llm_pool, "analyze", return_value=mock_result):
                await w.process_message(_make_msg(path=path))
            for call in w._channel.default_exchange.publish.call_args_list:
                msg = call.args[0]
                body = json.loads(msg.body)
                if "chunk_id" in body:
                    # Inference chunk messages are published by the entry
                    # stage fan-out (D3) and are not job envelopes.
                    continue
                assert "chunks" not in body
                assert body["job_id"] == "j1"
                assert "document_metadata" in body
        finally:
            os.unlink(path)


# --- Metadata ---

class TestMetadata:
    @pytest.mark.asyncio
    async def test_stores_image_metadata(self, mock_deps):
        from worker import ImageWorker
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            f.write(b"\x89PNG\r\n\x1a\n")
            path = f.name
        try:
            w = ImageWorker()
            w._channel = AsyncMock()
            mock_result = MagicMock()
            mock_result.extracted_text = "text"
            mock_result.description = "A cat photo"
            mock_result.language = "en"
            with patch.object(w.llm_pool, "analyze", return_value=mock_result), \
                 patch.object(w.llm_pool, "describe", return_value="A cat photo"):
                await w.process_message(_make_msg(path=path))
            metadata_calls = [
                c
                for c in mock_deps["redis_client"].setex.call_args_list
                if "metadata:image" in c.args[0]
            ]
            assert metadata_calls[0].args[1] == 86400
            stored = json.loads(metadata_calls[0].args[2])
            assert stored["language"] == "en"
            assert stored["description"] == "A cat photo"
        finally:
            os.unlink(path)

    @pytest.mark.asyncio
    async def test_chunks_stored_in_redis(self, mock_deps):
        from worker import ImageWorker
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            f.write(b"\x89PNG\r\n\x1a\n")
            path = f.name
        try:
            w = ImageWorker()
            w._channel = AsyncMock()
            mock_result = MagicMock()
            mock_result.extracted_text = "Hello world"
            mock_result.description = None
            mock_result.language = "en"
            with patch.object(w.llm_pool, "analyze", return_value=mock_result):
                await w.process_message(_make_msg(path=path))
            chunks_call = [c for c in mock_deps["redis_client"].set.call_args_list if "chunks" in str(c)]
            assert len(chunks_call) > 0
        finally:
            os.unlink(path)


# --- Profile-driven image calls (image-ocr-description) ---

class TestProfileDrivenImageCalls:
    @pytest.mark.asyncio
    async def test_full_profile_calls_analyze_and_describe(self, mock_deps):
        from worker import ImageWorker
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            f.write(b"\x89PNG\r\n\x1a\n")
            path = f.name
        try:
            w = ImageWorker()
            w._channel = AsyncMock()
            analyze_result = MagicMock()
            analyze_result.extracted_text = "OCR text"
            analyze_result.description = None
            analyze_result.language = "es"
            describe = MagicMock(return_value="A cat on a sofa")
            with patch.object(w.llm_pool, "analyze", return_value=analyze_result), \
                 patch.object(w.llm_pool, "describe", describe, create=True):
                await w.process_message(_make_msg(profile="full", path=path))
            describe.assert_called_once()
        finally:
            os.unlink(path)

    @pytest.mark.asyncio
    async def test_rag_profile_calls_analyze_only(self, mock_deps):
        from worker import ImageWorker
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            f.write(b"\x89PNG\r\n\x1a\n")
            path = f.name
        try:
            w = ImageWorker()
            w._channel = AsyncMock()
            analyze_result = MagicMock()
            analyze_result.extracted_text = "OCR text"
            analyze_result.description = None
            analyze_result.language = "es"
            describe = MagicMock(return_value="must not be used")
            with patch.object(w.llm_pool, "analyze", return_value=analyze_result), \
                 patch.object(w.llm_pool, "describe", describe, create=True):
                await w.process_message(_make_msg(profile="rag", path=path))
            describe.assert_not_called()
        finally:
            os.unlink(path)


class TestDescriptionSingleField:
    @pytest.mark.asyncio
    async def test_description_stored_once_and_never_embedded(
        self, mock_deps, monkeypatch, tmp_path
    ):
        import worker as image_worker
        from pkg.worker_common.artifact_store import FSStore

        store = FSStore(str(tmp_path))
        monkeypatch.setattr(image_worker, "STORE", store)
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            f.write(b"\x89PNG\r\n\x1a\n")
            path = f.name
        try:
            w = image_worker.ImageWorker()
            w._channel = AsyncMock()
            analyze_result = MagicMock()
            analyze_result.extracted_text = "OCR only"
            analyze_result.description = None
            analyze_result.language = "es"
            with patch.object(w.llm_pool, "analyze", return_value=analyze_result), \
                 patch.object(w.llm_pool, "describe", return_value="Semantic summary"):
                await w.process_message(_make_msg(profile="full", path=path))

            ref = store.put(b"OCR only")
            mock_deps["redis_client"].set.assert_any_call(
                "orchestrator:job:j1:text", ref
            )

            metadata_calls = [
                c
                for c in mock_deps["redis_client"].setex.call_args_list
                if "metadata:image" in c.args[0]
            ]
            assert len(metadata_calls) == 1
            assert metadata_calls[0].args[1] == 86400
            stored = json.loads(metadata_calls[0].args[2])
            assert stored["description"] == "Semantic summary"

            chunks_calls = [
                c
                for c in mock_deps["redis_client"].set.call_args_list
                if c.args[0].endswith(":chunks")
            ]
            chunks = json.loads(store.get(chunks_calls[0].args[1]).decode())
            embedded = " ".join(chunk["text"] for chunk in chunks)
            assert "Semantic summary" not in embedded
            assert embedded == "OCR only"
        finally:
            os.unlink(path)

    @pytest.mark.asyncio
    async def test_rag_profile_stores_null_description(self, mock_deps):
        from worker import ImageWorker
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            f.write(b"\x89PNG\r\n\x1a\n")
            path = f.name
        try:
            w = ImageWorker()
            w._channel = AsyncMock()
            analyze_result = MagicMock()
            analyze_result.extracted_text = "OCR text"
            analyze_result.description = None
            analyze_result.language = "es"
            with patch.object(w.llm_pool, "analyze", return_value=analyze_result):
                await w.process_message(_make_msg(profile="rag", path=path))

            metadata_calls = [
                c
                for c in mock_deps["redis_client"].setex.call_args_list
                if "metadata:image" in c.args[0]
            ]
            assert metadata_calls[0].args[1] == 86400
            stored = json.loads(metadata_calls[0].args[2])
            assert stored["description"] is None
        finally:
            os.unlink(path)


class TestDescriptionFailure:
    @pytest.mark.asyncio
    async def test_describe_failure_fails_the_job(self, mock_deps):
        """A `full` job cannot claim a non-null description on describe failure."""
        from worker import ImageWorker
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            f.write(b"\x89PNG\r\n\x1a\n")
            path = f.name
        try:
            w = ImageWorker()
            w._channel = AsyncMock()
            analyze_result = MagicMock()
            analyze_result.extracted_text = "OCR text"
            analyze_result.description = None
            analyze_result.language = "es"
            with patch.object(w.llm_pool, "analyze", return_value=analyze_result), \
                 patch.object(
                     w.llm_pool,
                     "describe",
                     side_effect=RuntimeError("describe down"),
                 ):
                with pytest.raises(RuntimeError, match="describe down"):
                    await w.process_message(_make_msg(profile="full", path=path))
            mock_deps["redis_client"].hset.assert_any_call(
                "orchestrator:job:j1:status", "status", "failed"
            )
        finally:
            os.unlink(path)
