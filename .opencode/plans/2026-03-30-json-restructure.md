# JSON Restructure Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Restructure the final JSON output so embeddings are embedded inside each chunk, entities use stable IDs, and inferences are nested per-chunk — while fixing the LLM prompt to synthesize facts instead of copying literal text.

**Architecture:** The completion-worker assembles the final JSON from Redis keys; it is the single point where the new structure is built. The entities-worker assigns stable entity IDs before storing them. The inference-worker's prompt is rewritten to discourage literal copying. No new Redis keys are added — only the shape of existing data changes.

**Tech Stack:** Python 3.11, Redis (JSON + MsgPack), RabbitMQ, vLLM (OpenAI-compatible), pytest, msgpack, hashlib (stdlib)

---

## File Map

| File | Change |
|------|--------|
| `cmd/entities-worker/worker.py` | Add `entity_id` field (sha256 hash) to each entity before storing in Redis |
| `cmd/inference-worker/worker.py` | Rewrite `extract_inferences()` prompt; rename `entities` → `entity_refs` in output |
| `cmd/completion-worker/worker.py` | Rewrite `finalize_job()` to build new JSON shape |
| `cmd/completion-worker/tests/test_finalize_job.py` | **NEW** — unit tests for the new JSON shape |
| `cmd/inference-worker/tests/test_inference_worker.py` | Extend existing tests to cover new `entity_refs` field and prompt change |
| `cmd/entities-worker/tests/test_entity_id.py` | **NEW** — unit test for entity ID assignment |

---

## Task 1: Entity ID assignment in entities-worker

**Goal:** Every entity stored in Redis has a stable `entity_id = sha256(f"{label}:{text.lower().strip()}")[:12]`.

**Files:**
- Modify: `cmd/entities-worker/worker.py` — add `_entity_id()` helper; call it before `json.dumps(all_entities)` at line 765
- Create: `cmd/entities-worker/tests/test_entity_id.py`

- [ ] **Step 1: Write failing test**

```python
# cmd/entities-worker/tests/test_entity_id.py
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import pytest
from worker import entity_id   # module-level helper, not method

def test_entity_id_deterministic():
    eid = entity_id("PER", "María García")
    assert eid == entity_id("PER", "  María García  ")   # strips whitespace
    assert eid == entity_id("PER", "maría garcía")       # case-insensitive
    assert len(eid) == 12

def test_entity_id_different_label():
    assert entity_id("PER", "centro") != entity_id("ORG", "centro")

def test_entity_id_hex():
    import re
    assert re.fullmatch(r"[0-9a-f]{12}", entity_id("LOC", "Zaragoza"))
```

- [ ] **Step 2: Run test — expect failure**

```bash
pytest cmd/entities-worker/tests/test_entity_id.py -v
# Expected: ImportError or AttributeError — entity_id not defined yet
```

- [ ] **Step 3: Add `entity_id` helper to worker.py**

Find the imports section of `cmd/entities-worker/worker.py` (top of file) and add `import hashlib`.  
Then, just before the `class EntitiesWorker` definition, add:

```python
def entity_id(label: str, text: str) -> str:
    """Return a stable 12-char hex ID for a (label, text) pair."""
    key = f"{label}:{text.lower().strip()}"
    return hashlib.sha256(key.encode()).hexdigest()[:12]
```

- [ ] **Step 4: Inject `entity_id` into every entity before storing**

In `cmd/entities-worker/worker.py`, find the line that stores entities (around line 765):
```python
self.redis_client.set(entities_key, json.dumps(all_entities))
```
Just before that line, add:
```python
for ent in all_entities:
    ent["entity_id"] = entity_id(ent.get("label", ""), ent.get("text", ""))
```

- [ ] **Step 5: Run tests — expect pass**

```bash
pytest cmd/entities-worker/tests/test_entity_id.py -v
# Expected: 3 passed
```

- [ ] **Step 6: Run existing entities tests — expect no regression**

```bash
pytest cmd/entities-worker/tests/ -v
# Expected: all pass
```

- [ ] **Step 7: Commit**

```bash
git add cmd/entities-worker/worker.py cmd/entities-worker/tests/test_entity_id.py
git commit -m "feat(entities): assign stable entity_id (sha256[:12]) to every entity"
```

---

## Task 2: Rename `entities` → `entity_refs` in inference-worker output + fix prompt

**Goal:** (a) Remove `entities` from the LLM prompt; instead, only pass `chunk_text`. (b) Rename the output field `entities` → `entity_refs`. (c) Rewrite the system prompt to explicitly forbid copying literal sentences.

**Files:**
- Modify: `cmd/inference-worker/worker.py` — `extract_inferences()` at line ~368
- Modify: `cmd/inference-worker/tests/test_inference_worker.py` — update assertions

- [ ] **Step 1: Update existing test to expect `entity_refs`, not `entities`**

In `cmd/inference-worker/tests/test_inference_worker.py`, find `test_extract_inferences_success` and update the mock LLM response body and assertions:

```python
# LLM now returns "entity_refs" instead of "entities"
mock_response.json.return_value = {
    "choices": [{
        "message": {
            "content": '[{"text": "Property value is 500000 EUR", "confidence": 0.95, "entity_refs": ["500000 EUR"]}]'
        }
    }]
}
# ...
assert inferences[0]["entity_refs"] == ["500000 EUR"]
# Old field must not be present
assert "entities" not in inferences[0]
assert "fact" not in inferences[0]
assert "source" not in inferences[0]
```

- [ ] **Step 2: Run test — expect failure (field still named `entities`)**

```bash
pytest cmd/inference-worker/tests/test_inference_worker.py::TestInferenceWorker::test_extract_inferences_success -v
```

- [ ] **Step 3: Rewrite `extract_inferences()` in worker.py**

Replace the `system_prompt`, `user_prompt`, and the validated output block in `extract_inferences()` (lines ~368–464).

New system prompt (replaces old one at line ~368):
```python
system_prompt = """You are a precise fact-extraction engine. Your task is to distill the key facts from a text passage into concise, self-contained statements.

Rules:
- Each fact MUST be a SYNTHESIZED, CONDENSED statement — never copy a literal sentence from the text.
- Each fact must be independently understandable without reading the original text.
- Mention specific values, names, dates, or amounts whenever they are in the text.
- Do NOT include vague or generic statements (e.g. "the document describes...").
- Respond ONLY with a valid JSON array. No explanation. No text outside the JSON.

Each object in the array must have exactly these fields:
- "text": condensed factual statement (your own words, not copied)
- "confidence": float between 0.0 and 1.0
- "entity_refs": list of entity name strings referenced in the fact"""
```

New user prompt (replaces old one at line ~377 — remove `entities_str`):
```python
user_prompt = f"""Extract up to {max_inferences} key facts from this text. Synthesize — do NOT copy sentences.

Text:
{chunk_text}

Respond with ONLY the JSON array:"""
```

Also remove the `entity_texts` / `entities_str` variables (lines ~359–362) — they are no longer used.

In the validation/annotation block (line ~454–464), rename `entities` → `entity_refs`:
```python
validated.append(
    {
        "text": inf.get("text", ""),
        "confidence": float(inf.get("confidence", 0.5)),
        "entity_refs": inf.get("entity_refs", inf.get("entities", [])),  # fallback for old LLM responses
    }
)
```

- [ ] **Step 4: Run test — expect pass**

```bash
pytest cmd/inference-worker/tests/test_inference_worker.py -v
# Expected: all pass
```

- [ ] **Step 5: Commit**

```bash
git add cmd/inference-worker/worker.py cmd/inference-worker/tests/test_inference_worker.py
git commit -m "feat(inference): rename entities->entity_refs, rewrite prompt to synthesize facts"
```

---

## Task 3: Restructure final JSON in completion-worker

**Goal:** Build the new JSON shape:
- `chunks[i]` includes `embeddings: [float]`, `entity_ids: [str]`, `inferences: [{text, confidence, entity_refs}]`
- Top-level `entities` is a dict `{entity_id: {label, text, confidence}}` (deduplicated by ID)
- Remove top-level `embeddings` key
- Remove top-level `micro_inferences` key

**Files:**
- Modify: `cmd/completion-worker/worker.py` — `finalize_job()` and `deduplicate_entities()`
- Create: `cmd/completion-worker/tests/test_finalize_job.py`

### Sub-task 3a: Unit tests for new JSON shape

- [ ] **Step 1: Create tests directory**

```bash
mkdir -p cmd/completion-worker/tests
touch cmd/completion-worker/tests/__init__.py
```

- [ ] **Step 2: Create test file**

```python
# cmd/completion-worker/tests/test_finalize_job.py
"""Unit tests for the new finalize_job() output structure."""
import json
import pytest
import sys
import os
import time
import msgpack
from unittest.mock import MagicMock, patch, call

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../.."))

from worker import CompletionWorker


@pytest.fixture
def worker():
    with patch("redis.from_url"):
        w = CompletionWorker()
        w.redis_client = MagicMock()
        w.redis_raw = MagicMock()
        return w


def _make_redis_pipeline(chunks, entities_raw, embeddings_dict, micro_inferences=None):
    """Build the tuple returned by pipe.execute() in finalize_job()."""
    meta = {"created_at": str(int(time.time()))}
    status_data = {"status": "processing"}
    text = "sample text"
    doc_meta = json.dumps({"mime_type": "application/pdf"})
    txt_meta = json.dumps({"language": "es"})
    chunks_json = json.dumps(chunks)
    entities_raw_json = json.dumps(entities_raw)
    source_classification_json = None
    micro_inferences_json = json.dumps(micro_inferences) if micro_inferences else None
    return (
        meta, status_data, text, doc_meta, txt_meta,
        chunks_json, entities_raw_json,
        source_classification_json, micro_inferences_json,
    )


def test_embeddings_nested_inside_chunks(worker):
    """Embeddings must be inside each chunk, not at top level."""
    chunks = [{"chunk_id": "chunk_000", "text": "hello"}]
    entities_raw = []
    embeddings_dict = {"chunk_000": [0.1, 0.2, 0.3]}

    worker.redis_client.pipeline.return_value.__enter__ = MagicMock()
    pipe = MagicMock()
    pipe.execute.return_value = _make_redis_pipeline(chunks, entities_raw, embeddings_dict)
    worker.redis_client.pipeline.return_value = pipe
    worker.redis_raw.get.return_value = msgpack.packb(embeddings_dict, use_bin_type=True)
    worker.redis_client.set = MagicMock()
    worker.redis_client.hset = MagicMock()

    with patch.object(worker, "save_results_to_file"), patch.object(worker, "send_webhook"), \
         patch.object(worker, "_publish_completed"):
        worker.finalize_job("job_abc")

    saved = json.loads(worker.redis_client.set.call_args_list[-1][0][1])

    assert "embeddings" not in saved, "Top-level 'embeddings' key must be removed"
    chunk = saved["chunks"][0]
    assert chunk["embeddings"] == [0.1, 0.2, 0.3]


def test_entities_dict_with_ids(worker):
    """Top-level entities must be a dict keyed by entity_id."""
    import hashlib
    chunks = [{"chunk_id": "chunk_000", "text": "hello"}]
    entities_raw = [
        {"label": "PER", "text": "María", "confidence": 0.9, "chunk_id": "chunk_000", "entity_id": "abc000000001"},
        {"label": "PER", "text": "María", "confidence": 0.8, "chunk_id": "chunk_000", "entity_id": "abc000000001"},
    ]

    pipe = MagicMock()
    pipe.execute.return_value = _make_redis_pipeline(chunks, entities_raw, {})
    worker.redis_client.pipeline.return_value = pipe
    worker.redis_raw.get.return_value = None
    worker.redis_client.set = MagicMock()
    worker.redis_client.hset = MagicMock()

    with patch.object(worker, "save_results_to_file"), patch.object(worker, "send_webhook"), \
         patch.object(worker, "_publish_completed"):
        worker.finalize_job("job_abc")

    saved = json.loads(worker.redis_client.set.call_args_list[-1][0][1])

    assert isinstance(saved["entities"], dict), "entities must be a dict"
    assert "abc000000001" in saved["entities"]
    assert saved["entities"]["abc000000001"]["confidence"] == 0.9  # highest wins
    assert len(saved["entities"]) == 1  # deduplicated


def test_inferences_nested_in_chunks(worker):
    """Inferences must appear inside each chunk, not at top level."""
    chunks = [{"chunk_id": "chunk_000", "text": "hello"}]
    entities_raw = []
    micro_inferences = [
        {"chunk_id": "chunk_000", "inferences": [
            {"text": "fact one", "confidence": 0.9, "entity_refs": ["María"]}
        ]}
    ]

    pipe = MagicMock()
    pipe.execute.return_value = _make_redis_pipeline(chunks, entities_raw, {}, micro_inferences)
    worker.redis_client.pipeline.return_value = pipe
    worker.redis_raw.get.return_value = None
    worker.redis_client.set = MagicMock()
    worker.redis_client.hset = MagicMock()

    with patch.object(worker, "save_results_to_file"), patch.object(worker, "send_webhook"), \
         patch.object(worker, "_publish_completed"):
        worker.finalize_job("job_abc")

    saved = json.loads(worker.redis_client.set.call_args_list[-1][0][1])

    assert "micro_inferences" not in saved, "Top-level micro_inferences must be removed"
    chunk = saved["chunks"][0]
    assert "inferences" in chunk
    assert chunk["inferences"][0]["text"] == "fact one"
    assert chunk["inferences"][0]["entity_refs"] == ["María"]


def test_entity_ids_in_chunks(worker):
    """Each chunk must have entity_ids list referencing global entity dict."""
    chunks = [{"chunk_id": "chunk_000", "text": "hello"}]
    entities_raw = [
        {"label": "PER", "text": "María", "confidence": 0.9, "chunk_id": "chunk_000", "entity_id": "abc000000001"},
    ]

    pipe = MagicMock()
    pipe.execute.return_value = _make_redis_pipeline(chunks, entities_raw, {})
    worker.redis_client.pipeline.return_value = pipe
    worker.redis_raw.get.return_value = None
    worker.redis_client.set = MagicMock()
    worker.redis_client.hset = MagicMock()

    with patch.object(worker, "save_results_to_file"), patch.object(worker, "send_webhook"), \
         patch.object(worker, "_publish_completed"):
        worker.finalize_job("job_abc")

    saved = json.loads(worker.redis_client.set.call_args_list[-1][0][1])
    chunk = saved["chunks"][0]
    assert "entity_ids" in chunk
    assert "abc000000001" in chunk["entity_ids"]


def test_no_top_level_micro_inferences_key(worker):
    """micro_inferences must NOT appear at top level."""
    chunks = [{"chunk_id": "chunk_000", "text": "hello"}]
    pipe = MagicMock()
    pipe.execute.return_value = _make_redis_pipeline(chunks, [], {})
    worker.redis_client.pipeline.return_value = pipe
    worker.redis_raw.get.return_value = None
    worker.redis_client.set = MagicMock()
    worker.redis_client.hset = MagicMock()

    with patch.object(worker, "save_results_to_file"), patch.object(worker, "send_webhook"), \
         patch.object(worker, "_publish_completed"):
        worker.finalize_job("job_abc")

    saved = json.loads(worker.redis_client.set.call_args_list[-1][0][1])
    assert "micro_inferences" not in saved
    assert "embeddings" not in saved
```

- [ ] **Step 2: Run tests — expect failures**

```bash
pytest cmd/completion-worker/tests/test_finalize_job.py -v
# Expected: FAIL — finalize_job() produces old structure
```

### Sub-task 3b: Rewrite `deduplicate_entities()` in completion-worker

The new dedup returns a **dict** `{entity_id: {label, text, confidence}}` instead of a list.

- [ ] **Step 3: Replace `deduplicate_entities()` method**

In `cmd/completion-worker/worker.py`, replace the entire `deduplicate_entities` method (lines ~215–272):

```python
def deduplicate_entities(self, entities: list) -> dict:
    """Deduplicate entities by entity_id, keeping highest confidence.

    Args:
        entities: List of entity dicts, each expected to have:
            - entity_id: stable 12-char hex ID
            - label, text, confidence, chunk_id (optional: start, end)

    Returns:
        Dict keyed by entity_id → {label, text, confidence}.
        Per-chunk fields (chunk_id, start, end) are stripped from the dict values.
    """
    if not entities:
        return {}

    result: dict = {}
    for ent in entities:
        eid = ent.get("entity_id")
        if not eid:
            # Fallback: generate on the fly if entity_id is missing (old data)
            import hashlib
            key = f"{ent.get('label', '')}:{ent.get('text', '').lower().strip()}"
            eid = hashlib.sha256(key.encode()).hexdigest()[:12]

        existing = result.get(eid)
        if existing is None or ent.get("confidence", 0) > existing.get("confidence", 0):
            result[eid] = {
                "label": ent.get("label", ""),
                "text": ent.get("text", ""),
                "confidence": ent.get("confidence", 0.0),
            }

    logger.info(
        f"Deduplicated entities: {len(entities)} raw → {len(result)} unique"
    )
    return result
```

### Sub-task 3c: Rewrite `finalize_job()` assembly block

- [ ] **Step 4: Replace the results assembly block in `finalize_job()`**

In `cmd/completion-worker/worker.py`, find the block starting at line ~492 and replace through line ~548:

**Old code (to replace):**
```python
            embeddings_raw = msgpack.unpackb(embeddings_raw_bytes, raw=False) if embeddings_raw_bytes else {}
            embeddings = {"model": "BAAI/bge-m3", "dimension": 1024, **embeddings_raw}

            # Read RAW entities from entities-worker (before dedup)
            entities_raw = json.loads(entities_raw_json) if entities_raw_json else []

            # Apply deduplication at the end (now that we have all entities from all chunks)
            entities = self.deduplicate_entities(entities_raw) if entities_raw else []

            logger.info(
                f"Entities: {len(entities_raw)} raw → {len(entities)} after dedup"
            )

            # Parse source classification and micro inferences if present
            source_classification = None
            micro_inferences = None

            try:
                if source_classification_json:
                    source_classification = json.loads(source_classification_json)
            except json.JSONDecodeError as e:
                logger.warning(f"Failed to parse source_classification JSON: {e}")

            try:
                if micro_inferences_json:
                    micro_inferences = json.loads(micro_inferences_json)
                    # micro_inferences is now: [{chunk_id: "...", inferences: [...]}, ...]
                    # Validate structure
                    if not isinstance(micro_inferences, list):
                        logger.warning(
                            f"micro_inferences is not a list, got {type(micro_inferences)}"
                        )
                        micro_inferences = None
            except json.JSONDecodeError as e:
                logger.warning(f"Failed to parse micro_inferences JSON: {e}")
                micro_inferences = None

            # Build results dict with optional inference fields
            results = {
                "job_id": job_id,
                "status": "completed",
                "created_at": created_at,
                "completed_at": completed_at,
                "document_metadata": document_metadata,
                "text_metadata": text_metadata,
                "chunks": chunks,
                "embeddings": embeddings,
                "entities": entities,
            }

            # Add optional inference fields if present
            if source_classification is not None:
                results["source_classification"] = source_classification
            if micro_inferences is not None:
                results["micro_inferences"] = micro_inferences
```

**New code:**
```python
            # --- Embeddings: {chunk_id: [float]} ---
            embeddings_by_chunk: dict = {}
            if embeddings_raw_bytes:
                raw = msgpack.unpackb(embeddings_raw_bytes, raw=False)
                # raw is {chunk_id: [float]} — strip non-chunk keys like "model"/"dimension" if any
                embeddings_by_chunk = {k: v for k, v in raw.items() if isinstance(v, list)}

            # --- Entities: deduplicate → global dict {entity_id: {label, text, confidence}} ---
            entities_raw = json.loads(entities_raw_json) if entities_raw_json else []
            entities_dict = self.deduplicate_entities(entities_raw) if entities_raw else {}

            logger.info(
                f"Entities: {len(entities_raw)} raw → {len(entities_dict)} unique (by entity_id)"
            )

            # --- Build per-chunk entity_ids index ---
            entity_ids_by_chunk: dict = {}  # {chunk_id: [entity_id]}
            for ent in entities_raw:
                cid = ent.get("chunk_id")
                eid = ent.get("entity_id")
                if cid and eid:
                    entity_ids_by_chunk.setdefault(cid, [])
                    if eid not in entity_ids_by_chunk[cid]:
                        entity_ids_by_chunk[cid].append(eid)

            # --- Micro-inferences: parse and index by chunk_id ---
            inferences_by_chunk: dict = {}  # {chunk_id: [inference]}
            source_classification = None

            try:
                if source_classification_json:
                    source_classification = json.loads(source_classification_json)
            except json.JSONDecodeError as e:
                logger.warning(f"Failed to parse source_classification JSON: {e}")

            try:
                if micro_inferences_json:
                    micro_inferences_list = json.loads(micro_inferences_json)
                    if isinstance(micro_inferences_list, list):
                        for item in micro_inferences_list:
                            cid = item.get("chunk_id")
                            if cid:
                                inferences_by_chunk[cid] = item.get("inferences", [])
                    else:
                        logger.warning(
                            f"micro_inferences is not a list, got {type(micro_inferences_list)}"
                        )
            except json.JSONDecodeError as e:
                logger.warning(f"Failed to parse micro_inferences JSON: {e}")

            # --- Enrich chunks: embed embeddings, entity_ids, inferences ---
            enriched_chunks = []
            for chunk in chunks:
                cid = chunk.get("chunk_id", "")
                enriched = dict(chunk)  # shallow copy — preserve all existing fields
                enriched["embeddings"] = embeddings_by_chunk.get(cid, [])
                enriched["entity_ids"] = entity_ids_by_chunk.get(cid, [])
                if cid in inferences_by_chunk:
                    enriched["inferences"] = inferences_by_chunk[cid]
                enriched_chunks.append(enriched)

            # --- Final result ---
            results = {
                "job_id": job_id,
                "status": "completed",
                "created_at": created_at,
                "completed_at": completed_at,
                "document_metadata": document_metadata,
                "text_metadata": text_metadata,
                "chunks": enriched_chunks,
                "entities": entities_dict,
            }

            if source_classification is not None:
                results["source_classification"] = source_classification
```

- [ ] **Step 5: Fix log message** — the log at line ~551 references `entities` (list) and `micro_inferences`; update it:

```python
            # Log completion stats
            total_inferences = sum(len(c.get("inferences", [])) for c in enriched_chunks)
            log_message = (
                f"Job {job_id} finalized: chunks={len(enriched_chunks)}, "
                f"entities={len(entities_dict)}, inferences={total_inferences}"
            )
            if source_classification:
                log_message += f", source_type={source_classification.get('document_type', 'unknown')}"
            logger.info(log_message)
```

- [ ] **Step 6: Run new tests — expect pass**

```bash
pytest cmd/completion-worker/tests/test_finalize_job.py -v
# Expected: 5 passed
```

- [ ] **Step 7: Commit**

```bash
git add cmd/completion-worker/worker.py cmd/completion-worker/tests/test_finalize_job.py
git commit -m "feat(completion): restructure JSON — embeddings/inferences inside chunks, entities as ID dict"
```

---

## Task 4: Full test suite — no regressions

- [ ] **Step 1: Run all Python tests**

```bash
make test-python
# or manually:
pytest cmd/completion-worker/tests/ cmd/inference-worker/tests/ cmd/entities-worker/tests/ -v
```

Expected: all pass. Fix any regression before proceeding.

- [ ] **Step 2: Validate against real output**

If `/tmp/ee.json` exists (production output from before the change), compare the new structure mentally:
- `chunks[0]` must have `embeddings`, `entity_ids`, optionally `inferences`
- No top-level `embeddings` key
- `entities` is a dict not a list

```bash
# If a local test job can be triggered:
# make docker-up && curl -X POST http://localhost:8080/jobs -d '{"url":"...","features":["inferences"]}' ...
```

- [ ] **Step 3: Commit (if any fixes were needed)**

```bash
git add -A
git commit -m "fix: address test regressions after JSON restructure"
```

---

## Out of Scope

- Changing RabbitMQ message schemas (only the Redis output shape changes)
- Adding new Redis keys
- Changing the embeddings-worker storage format (reshaping happens in completion-worker)
- UI/API changes
- `entities_deduped` read-only lookup key (not requested for this plan)
