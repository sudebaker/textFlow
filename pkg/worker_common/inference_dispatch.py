"""Inference dispatch for entry-stage workers (D3 / W2).

Micro-inference chunks are dispatched by the ENTRY stage (extraction, audio,
image) instead of the entities step, so a `rag` job — which has no entities
step — still produces inferences.

Race fix kept from entities_worker: the `inferences:remaining`/`:total`
counters MUST be written BEFORE the first chunk message is published,
otherwise a consumer DECRs a non-existent key and Redis creates it as -1.
"""

import json
import logging
import os
from typing import Any, Callable, Dict, List

logger = logging.getLogger(__name__)

INFERENCES_QUEUE = os.getenv("INFERENCES_QUEUE", "inferences")
INFERENCES_TTL_SECONDS = 86400


def inference_counter_key(job_id: str, suffix: str) -> str:
    """Redis key for the per-job inference counter (`remaining` / `total`)."""
    return f"orchestrator:job:{job_id}:inferences:{suffix}"


def resolve_source_type(redis_client, job_id: str) -> str:
    """Document source type written by the extraction stage, else "generico"."""
    try:
        raw = redis_client.get(f"orchestrator:job:{job_id}:source_classification")
        if raw:
            return json.loads(raw).get("document_type") or "generico"
    except Exception as e:
        logger.warning(f"Could not resolve source type for job {job_id}: {e}")
    return "generico"


def plan_inference_messages(
    chunks: List[Dict[str, Any]],
    job_id: str,
    source_type: str = "generico",
    entities_by_chunk: Dict[str, List[Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    """One inference message per non-empty chunk.

    Entry stages dispatch before entities run, so `entities` defaults to an
    empty list per chunk (inferences no longer depend on the entities step).
    """
    valid_chunks = [c for c in chunks if str(c.get("text") or "").strip()]
    total = len(valid_chunks)
    return [
        {
            "job_id": job_id,
            "chunk_id": chunk.get("chunk_id"),
            "chunk_text": chunk.get("text", ""),
            "entities": (entities_by_chunk or {}).get(chunk.get("chunk_id"), []),
            "source_type": source_type,
            "total_chunks": total,
        }
        for chunk in valid_chunks
    ]


async def dispatch_inference_chunks(
    *,
    publish: Callable[[Dict[str, Any]], Any],
    set_counter: Callable[[str, int], None],
    job_id: str,
    chunks: List[Dict[str, Any]],
    source_type: str = "generico",
) -> int:
    """Set the job counters, then publish one message per non-empty chunk.

    Args:
        publish: awaitable callable that publishes a message to the inferences
            queue (each worker wires its own aio_pika exchange).
        set_counter: sync callable writing a counter with the job TTL.
        job_id: job identifier.
        chunks: extracted chunks (entry-stage output).
        source_type: document type used to shape the inference prompt.

    Returns:
        Number of chunk messages dispatched (0 when there is nothing to do).
    """
    messages = plan_inference_messages(chunks, job_id, source_type)
    if not messages:
        logger.info(f"No non-empty chunks to dispatch inferences for job {job_id}")
        return 0

    total = len(messages)
    set_counter("remaining", total)
    set_counter("total", total)
    for message in messages:
        await publish(message)

    logger.info(f"Dispatched {total} inference chunks for job {job_id}")
    return total
