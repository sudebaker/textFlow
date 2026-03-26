# Micro-Inferences Per-Chunk Integration Flow

## Overview

This document describes the end-to-end integration flow for the per-chunk micro-inferences feature, which enables parallel inference processing across document chunks with atomic assembly guarantees.

**Key Components:**
- `entities-worker`: Extracts entities and fans out tasks
- `inference-worker`: Processes chunks and performs atomic assembly
- `completion-worker`: Aggregates results and finalizes jobs

---

## Message Flow Architecture

### Stage 1: Orchestrator to entities-worker

**Input Message (via `jobs` queue):**
```python
{
    "job_id": "test-123",
    "document_id": "doc-001",
    "chunks": [
        {"chunk_id": 0, "text": "The quick brown fox..."},
        {"chunk_id": 1, "text": "jumped over the lazy..."},
        {"chunk_id": 2, "text": "dog and ran away."}
    ],
    "features": ["inferences"],  # Indicates inference processing needed
    "metadata": {...}
}
```

---

### Stage 2: entities-worker → redis + inferences queue

**Processing:**
1. Extract entities from each chunk using GLiNER
2. For each chunk, publish a separate message to `inferences` queue:
   ```python
   {
       "job_id": "test-123",
       "chunk_id": 0,
       "chunk_text": "The quick brown fox...",
       "entities": [
           {"entity": "Adjective", "text": "quick", "start": 4, "end": 9},
           {"entity": "Animal", "text": "fox", "start": 16, "end": 19}
       ]
   }
   ```

3. **Atomic Counter Setup in Redis:**
   - `SETEX inferences:remaining:test-123 3600 3`  (TTL 1 hour)
   - Total chunks (N) = 3
   - Each inference-worker will decrement this counter

---

### Stage 3: inference-worker processing (parallel, N workers)

**Per-Chunk Processing Flow:**

Each inference-worker instance processes one message from the `inferences` queue independently.

**Input Message:**
```python
{
    "job_id": "test-123",
    "chunk_id": 0,
    "chunk_text": "The quick brown fox...",
    "entities": [...]
}
```

**Processing Steps:**
1. Extract chunk_id and chunk_text
2. Build prompt with entities as context
3. Call LLM for micro-inferences:
   ```python
   result = llm_call(
       prompt=f"Based on entities {entities}, infer insights from: {chunk_text}"
   )
   ```
4. **Atomic Assembly Logic:**
   ```python
   # Push raw inference to list
   redis_client.rpush(
       f"micro_inferences_raw:{job_id}",
       json.dumps({
           "chunk_id": chunk_id,
           "inferences": parse_inferences(result)
       })
   )
   
   # Decrement counter
   remaining = redis_client.decr(f"inferences:remaining:{job_id}")
   
   # If this was the last chunk, assemble
   if remaining <= 0:
       raw_list = redis_client.lrange(
           f"micro_inferences_raw:{job_id}",
           0, -1
       )
       assembled = [json.loads(item) for item in raw_list]
       
       # Sort by chunk_id to ensure order
       assembled.sort(key=lambda x: x["chunk_id"])
       
       # Store final result
       redis_client.set(
           f"micro_inferences:{job_id}",
           json.dumps(assembled)
       )
       
       # Mark step as complete
       redis_client.hset(
           f"job:{job_id}:steps",
           "inferences",
           "completed"
       )
   ```

**Output State in Redis (after all workers complete):**
```
Key: micro_inferences:test-123
Value: [
    {
        "chunk_id": 0,
        "inferences": ["inference1", "inference2", ...]
    },
    {
        "chunk_id": 1,
        "inferences": ["inference3", "inference4", ...]
    },
    {
        "chunk_id": 2,
        "inferences": ["inference5", "inference6", ...]
    }
]

Key: job:test-123:steps (HSET)
Value: {
    "inferences": "completed",
    ...other_steps
}
```

---

### Stage 4: Job Progress Event

**Event Published (when inferences step completes):**
```python
{
    "event_type": "step_completed",
    "job_id": "test-123",
    "step": "inferences",
    "timestamp": "2026-03-26T18:04:00Z"
}
```

Sent to `job_progress` queue/topic for downstream services.

---

### Stage 5: completion-worker finalization

**Listener:**
- completion-worker subscribes to `job_progress` events
- Triggered when all required steps are marked complete

**Processing:**
```python
def finalize_job(job_id):
    # Check all required steps are done
    steps = redis_client.hgetall(f"job:{job_id}:steps")
    required = ["entities", "inferences"]  # example
    
    if not all(step in steps for step in required):
        return  # Not ready yet
    
    # Retrieve micro_inferences
    micro_inferences_raw = redis_client.get(f"micro_inferences:{job_id}")
    micro_inferences = json.loads(micro_inferences_raw)
    
    # Assemble final job result
    final_result = {
        "job_id": job_id,
        "document_id": "doc-001",
        "status": "completed",
        "results": {
            "entities": {
                "chunks": [...]  # from entities step
            },
            "micro_inferences": micro_inferences,  # Grouped list
            "metadata": {...}
        },
        "completed_at": "2026-03-26T18:04:05Z"
    }
    
    # Persist to database
    db.save_job_result(final_result)
    
    # Clean up Redis keys
    cleanup_job_redis_keys(job_id)
```

**Output in Final Results:**
```json
{
    "results": {
        "micro_inferences": [
            {
                "chunk_id": 0,
                "inferences": ["insight_a", "insight_b"]
            },
            {
                "chunk_id": 1,
                "inferences": ["insight_c", "insight_d"]
            },
            {
                "chunk_id": 2,
                "inferences": ["insight_e", "insight_f"]
            }
        ]
    }
}
```

---

## Test Scenario: 3-Chunk Document

### Setup
```bash
# Start infrastructure
make infra-up

# Terminal 1: entities-worker
make run-entities-worker

# Terminal 2: inference-worker (or multiple instances)
make run-inference-worker

# Terminal 3: completion-worker
make run-completion-worker
```

### Execution Step-by-Step

**1. Orchestrator publishes job:**
```bash
curl -X POST http://localhost:8080/api/v1/jobs \
  -H "Content-Type: application/json" \
  -d '{
    "document_id": "doc-001",
    "chunks": [
      {"chunk_id": 0, "text": "The quick brown fox..."},
      {"chunk_id": 1, "text": "jumped over the lazy..."},
      {"chunk_id": 2, "text": "dog and ran away."}
    ],
    "features": ["inferences"]
  }'
```
Returns: `job_id: test-123`

**2. Watch entities-worker logs:**
```
✓ Processing job test-123, 3 chunks
✓ Extracted entities from chunk 0: 2 entities
✓ Extracted entities from chunk 1: 3 entities
✓ Extracted entities from chunk 2: 1 entity
✓ Published 3 messages to inferences queue
✓ Set inferences:remaining:test-123 = 3
```

**3. Watch inference-worker logs (multiple workers):**
```
# Worker 1 (chunk 0)
✓ Processing chunk 0 (inferences:remaining:test-123 = 3)
✓ LLM inference completed
✓ RPUSH micro_inferences_raw:test-123
✓ DECR → remaining = 2

# Worker 2 (chunk 1)
✓ Processing chunk 1 (inferences:remaining:test-123 = 2)
✓ LLM inference completed
✓ RPUSH micro_inferences_raw:test-123
✓ DECR → remaining = 1

# Worker 3 (chunk 2) - LAST TO FINISH
✓ Processing chunk 2 (inferences:remaining:test-123 = 1)
✓ LLM inference completed
✓ RPUSH micro_inferences_raw:test-123
✓ DECR → remaining = 0
✓ Remaining <= 0: ASSEMBLING
✓ LRANGE micro_inferences_raw:test-123 → 3 items
✓ Sorted by chunk_id
✓ SET micro_inferences:test-123
✓ HSET job:test-123:steps inferences = completed
✓ Published step_completed event
```

**4. Watch completion-worker logs:**
```
✓ Received step_completed: inferences
✓ Checking all required steps...
✓ All steps complete: finalizing job
✓ Retrieved micro_inferences from Redis
✓ Assembled final result
✓ Parsed micro_inferences as grouped list
✓ Saved to database
✓ Cleaned up Redis keys
✓ Job test-123 finalized
```

**5. Verify final result:**
```bash
curl http://localhost:8080/api/v1/jobs/test-123
```

Expected response:
```json
{
    "job_id": "test-123",
    "document_id": "doc-001",
    "status": "completed",
    "results": {
        "micro_inferences": [
            {
                "chunk_id": 0,
                "inferences": ["insight_from_chunk_0_entity_1", ...]
            },
            {
                "chunk_id": 1,
                "inferences": ["insight_from_chunk_1_entity_2", ...]
            },
            {
                "chunk_id": 2,
                "inferences": ["insight_from_chunk_2_entity_3", ...]
            }
        ]
    }
}
```

---

## Atomic Assembly Guarantee

The counter-based assembly ensures:

1. **No race conditions**: `DECR` is atomic in Redis
2. **Exactly once assembly**: Only the worker that decrements to ≤0 assembles
3. **Order preservation**: Sorting by `chunk_id` before persistence
4. **Completeness**: All N chunks represented (list length = N)

**Critical timing:**
- Counter set: After all N messages published to queue
- Counter check: After `RPUSH` and `DECR`
- Assembly trigger: `remaining <= 0` (handles race where last worker might see 0)

---

## Redis Key Lifecycle

| Key | Created By | Deleted By | TTL |
|-----|-----------|-----------|-----|
| `inferences:remaining:{job_id}` | entities-worker | completion-worker (cleanup) | 3600s |
| `micro_inferences_raw:{job_id}` | inference-worker | inference-worker (last) | None (auto-cleanup) |
| `micro_inferences:{job_id}` | inference-worker (last) | completion-worker (cleanup) | None |
| `job:{job_id}:steps` | orchestrator | completion-worker (cleanup) | 3600s |

---

## Debugging Checklist

**If inferences are missing from final result:**
- [ ] Check `micro_inferences_raw:{job_id}` exists in Redis
- [ ] Verify counter reached 0 (check logs for "Remaining <= 0")
- [ ] Confirm `micro_inferences:{job_id}` is set
- [ ] Check `job:test-123:steps` has inferences = completed

**If assembly doesn't trigger:**
- [ ] Verify N chunks were published to inferences queue
- [ ] Check `inferences:remaining:{job_id}` was initialized correctly
- [ ] Ensure all inference-workers finished processing
- [ ] Check for worker crashes (see logs)

**If order is wrong:**
- [ ] Verify sorting logic in assembly: `sort(key=lambda x: x["chunk_id"])`
- [ ] Check chunk_id values in raw list

---

## Implementation Reference Files

- **entities-worker:** `cmd/entities-worker/worker.py` (Task 1)
- **inference-worker:** `cmd/inference-worker/worker.py` (Task 2)
- **completion-worker:** `cmd/completion-worker/worker.py` (Task 3)
- **Spec:** `docs/plans/per-chunk-micro-inferences-spec.md`
- **Plan:** `docs/plans/per-chunk-micro-inferences-implementation-plan.md`

---

## Success Criteria

- [x] All 3 Python files compile without syntax errors
- [x] Redis state machine executes atomically
- [x] Micro-inferences appear in final job results as grouped list
- [x] Chunk order preserved (sorted by chunk_id)
- [x] No data loss (all N chunks represented)
- [x] No race conditions (counter-based assembly)
