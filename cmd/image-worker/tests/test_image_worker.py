import asyncio
import json
import os
import tempfile
from unittest.mock import MagicMock, patch, AsyncMock

import pytest


@pytest.fixture
def mock_deps():
    with patch("pkg.worker_common.base.redis") as mock_redis_mod, \
         patch("pkg.worker_common.base.EventBus") as mock_event_bus_cls:
        mock_redis_client = MagicMock()
        mock_redis_client.hset = MagicMock()
        mock_redis_client.set = MagicMock()
        mock_redis_client.get = MagicMock()
        mock_redis_mod.from_url.return_value = mock_redis_client
        mock_event_bus = MagicMock()
        mock_event_bus_cls.return_value = mock_event_bus
        yield {
            "redis_client": mock_redis_client,
            "event_bus": mock_event_bus,
        }


def _make_msg(job_id="j1", filename="test.png", entity_types=None, features=None, path=None):
    body = {
        "job_id": job_id,
        "document_path": path or "/tmp/test.png",
        "filename": filename,
    }
    if entity_types:
        body["entity_types"] = entity_types
    if features:
        body["features"] = features
    return body


# --- Basic tests ---

class TestImageWorkerInit:
    def test_has_worker_name(self, mock_deps):
        from image_worker import ImageWorker
        w = ImageWorker()
        assert w.worker_name == "image-worker"

    def test_has_queue_name(self, mock_deps):
        from image_worker import ImageWorker
        w = ImageWorker()
        assert w.queue_name == os.getenv("IMAGE_QUEUE", "image")

    def test_creates_llm_pool(self, mock_deps):
        from image_worker import ImageWorker
        w = ImageWorker()
        assert w.llm_pool is not None


# --- Success path ---

class TestProcessMessageSuccess:
    @pytest.mark.asyncio
    async def test_sets_status_analyzing(self, mock_deps):
        from image_worker import ImageWorker
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
        import image_worker
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
    async def test_publishes_to_downstream_queues(self, mock_deps):
        from image_worker import ImageWorker
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
                await w.process_message(_make_msg(features=["inferences"], path=path))
            assert w._channel.default_exchange.publish.call_count == 4
        finally:
            os.unlink(path)

    @pytest.mark.asyncio
    async def test_publishes_without_inferences_queue(self, mock_deps):
        from image_worker import ImageWorker
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
            assert w._channel.default_exchange.publish.call_count == 3
        finally:
            os.unlink(path)

    @pytest.mark.asyncio
    async def test_steps_marked_completed(self, mock_deps):
        from image_worker import ImageWorker
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
        from image_worker import ImageWorker
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
        from image_worker import ImageWorker
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
        from image_worker import ImageWorker
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
        from image_worker import ImageWorker
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
        from image_worker import ImageWorker
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
            assert w._channel.default_exchange.publish.call_count == 3
        finally:
            os.unlink(path)


# --- Metadata ---

class TestMetadata:
    @pytest.mark.asyncio
    async def test_stores_image_metadata(self, mock_deps):
        from image_worker import ImageWorker
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
            with patch.object(w.llm_pool, "analyze", return_value=mock_result):
                await w.process_message(_make_msg(path=path))
            stored = json.loads(mock_deps["redis_client"].set.call_args_list[-3][0][1])
            assert stored["language"] == "en"
            assert stored["description"] == "A cat photo"
        finally:
            os.unlink(path)

    @pytest.mark.asyncio
    async def test_chunks_stored_in_redis(self, mock_deps):
        from image_worker import ImageWorker
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
