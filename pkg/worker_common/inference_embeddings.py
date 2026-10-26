"""Shared helper for generating inference embeddings.

Used by embeddings-worker and completion-worker to avoid code duplication.
"""

from typing import Any, Callable, Dict, List, Optional


def generate_inference_embeddings(
    inferences_by_chunk: Dict[str, List[Dict[str, Any]]],
    embed_fn: Callable[[List[str]], Any],
    logger: Any,
) -> Dict[str, Dict[str, List[float]]]:
    """Generate embeddings for inference texts grouped by chunk.

    Args:
        inferences_by_chunk: Dict mapping chunk_id to list of inference dicts.
            Each inference dict must have a "text" key.
        embed_fn: Callable that takes a list of strings and returns embeddings.
            Can return numpy arrays, torch tensors, or lists.
        logger: Logger instance for debug/warning messages.

    Returns:
        Dict mapping chunk_id -> {inference_idx: embedding_vector}
    """
    if not inferences_by_chunk:
        return {}

    inference_embeddings: Dict[str, Dict[str, List[float]]] = {}
    attempted = 0
    failed = 0
    last_error: Optional[Exception] = None

    for chunk_id, inferences in inferences_by_chunk.items():
        if not inferences:
            continue

        texts = [inf.get("text", "") or "" for inf in inferences]
        if not any(texts):
            continue

        attempted += 1
        try:
            embeddings = embed_fn(texts)

            chunk_embeddings: Dict[str, List[float]] = {}
            for idx, embedding in enumerate(embeddings):
                chunk_embeddings[f"inference_{idx}"] = (
                    embedding.tolist() if hasattr(embedding, "tolist") else list(embedding)
                )

            inference_embeddings[chunk_id] = chunk_embeddings
            logger.debug(
                f"Generated {len(chunk_embeddings)} inference embeddings for chunk {chunk_id}"
            )
        except Exception as e:
            failed += 1
            last_error = e
            logger.warning(f"Failed to generate embeddings for chunk {chunk_id}: {e}")

    if attempted and failed == attempted:
        # Total failure must fail the job: there is no local fallback.
        raise RuntimeError(f"All inference embeddings failed: {last_error}")

    return inference_embeddings
