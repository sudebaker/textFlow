"""Unified Stage interface for pipeline workers.

Workers expose message-processing callables with heterogeneous signatures:
- Synchronous workers (BaseWorker): ``process_message(message) -> Any``
- Async workers (BaseAsyncWorker): ``async process_message(message) -> None``
- extraction-worker: ``_process_message_async(message, channel)``

This module defines a thin contract layer — ``Stage`` and ``StageResult`` —
that documents the unified ``execute(context) -> StageResult`` contract and
provides helpers to adapt existing workers without rewriting them. It does
NOT replace the existing worker implementations or their consumption loops.
"""

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional


@dataclass
class StageResult:
    """Result of executing a pipeline stage."""

    status: str  # "success" | "failed" | "cancelled" | "skipped"
    job_id: Optional[str] = None
    stage: Optional[str] = None
    data: Dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None

    @classmethod
    def success(cls, job_id=None, stage=None, data=None):
        return cls(status="success", job_id=job_id, stage=stage, data=data or {})

    @classmethod
    def failed(cls, job_id=None, stage=None, error=None):
        return cls(status="failed", job_id=job_id, stage=stage, error=error)

    @classmethod
    def cancelled(cls, job_id=None, stage=None):
        return cls(status="cancelled", job_id=job_id, stage=stage)


class Stage:
    """Unified interface for pipeline stages.

    A Stage wraps a worker's message-processing callable behind a single
    contract: execute(context) -> StageResult. Both synchronous and async
    workers can be adapted. This is a thin contract layer — it does NOT
    replace the existing worker implementations.
    """

    def __init__(self, name: str, fn: Callable, is_async: bool = False):
        self.name = name
        self.fn = fn
        self.is_async = is_async

    def execute(self, message: Dict[str, Any]) -> StageResult:
        """Execute the stage synchronously. For async stages, callers must
        use execute_async instead."""
        if self.is_async:
            raise TypeError(f"Stage '{self.name}' is async; use execute_async()")
        try:
            result = self.fn(message)
            return StageResult.success(stage=self.name, data={"result": result})
        except Exception as e:
            return StageResult.failed(stage=self.name, error=str(e))

    async def execute_async(self, message: Dict[str, Any]) -> StageResult:
        """Execute the stage asynchronously."""
        if not self.is_async:
            # Fall back to sync execution in a thread pool.
            import asyncio

            return await asyncio.to_thread(self.execute, message)
        try:
            result = await self.fn(message)
            return StageResult.success(stage=self.name, data={"result": result})
        except Exception as e:
            return StageResult.failed(stage=self.name, error=str(e))


def stage_from_worker(worker: Any, name: str) -> Stage:
    """Adapt a worker instance to a Stage.

    Detects whether the worker's process_message is a coroutine function
    (async) or a plain function (sync) and wraps it accordingly.
    """
    import inspect

    fn = getattr(worker, "process_message", None)
    if fn is None:
        # extraction-worker uses _process_message_async
        fn = getattr(worker, "_process_message_async", None)
    if fn is None:
        raise AttributeError(
            f"Worker {name} has no process_message/_process_message_async"
        )
    is_async = inspect.iscoroutinefunction(fn)
    return Stage(name=name, fn=fn, is_async=is_async)
