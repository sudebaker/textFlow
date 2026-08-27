"""Tests for the unified Stage interface (pkg/worker_common/stage.py)."""

# Standard library
import asyncio

# Third-party
import pytest

# Local
from pkg.worker_common.stage import Stage, StageResult, stage_from_worker


class SyncWorker:
    def process_message(self, message):
        return {"ok": True}


class AsyncWorker:
    async def process_message(self, message):
        return {"ok": True}


class ExtractionStyleWorker:
    async def _process_message_async(self, message, channel=None):
        return {"ok": True}


class NoHandlerWorker:
    pass


def test_sync_execute_returns_success():
    stage = Stage(name="sync", fn=lambda m: {"ok": True})
    result = stage.execute({"job_id": "1"})
    assert result.status == "success"
    assert result.stage == "sync"
    assert result.data["result"] == {"ok": True}


def test_sync_execute_returns_failed_on_exception():
    def boom(message):
        raise ValueError("boom")

    stage = Stage(name="sync", fn=boom)
    result = stage.execute({"job_id": "1"})
    assert result.status == "failed"
    assert result.error == "boom"


def test_sync_execute_raises_typeerror_for_async_stage():
    stage = Stage(name="async", fn=AsyncWorker().process_message, is_async=True)
    with pytest.raises(TypeError):
        stage.execute({"job_id": "1"})


def test_stage_from_worker_detects_sync():
    stage = stage_from_worker(SyncWorker(), "sync")
    assert stage.is_async is False
    result = stage.execute({"job_id": "1"})
    assert result.status == "success"


def test_stage_from_worker_detects_async():
    stage = stage_from_worker(AsyncWorker(), "async")
    assert stage.is_async is True
    result = asyncio.run(stage.execute_async({"job_id": "1"}))
    assert result.status == "success"


def test_stage_from_worker_falls_back_to_process_message_async():
    stage = stage_from_worker(ExtractionStyleWorker(), "extraction")
    assert stage.is_async is True


def test_stage_from_worker_raises_without_handler():
    with pytest.raises(AttributeError):
        stage_from_worker(NoHandlerWorker(), "none")


def test_execute_async_returns_success():
    stage = Stage(name="async", fn=AsyncWorker().process_message, is_async=True)
    result = asyncio.run(stage.execute_async({"job_id": "1"}))
    assert result.status == "success"
    assert result.data["result"] == {"ok": True}


def test_execute_async_returns_failed_on_exception():
    async def boom(message):
        raise RuntimeError("async boom")

    stage = Stage(name="async", fn=boom, is_async=True)
    result = asyncio.run(stage.execute_async({"job_id": "1"}))
    assert result.status == "failed"
    assert result.error == "async boom"


def test_execute_async_falls_back_to_sync_in_thread():
    stage = Stage(name="sync", fn=lambda m: {"ok": True}, is_async=False)
    result = asyncio.run(stage.execute_async({"job_id": "1"}))
    assert result.status == "success"


def test_stage_result_factories():
    assert StageResult.success().status == "success"
    assert StageResult.failed(error="e").status == "failed"
    assert StageResult.failed(error="e").error == "e"
    assert StageResult.cancelled().status == "cancelled"
