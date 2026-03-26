# Micro-Inferences Per-Chunk Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Refactor inference-worker and entities-worker to process micro-inferences per chunk with parallel execution via atomic Redis counter, eliminating 2000-char truncation and enabling event-driven scaling.

**Architecture:** entities-worker fans out N messages (one per chunk with entities) to the inferences queue. inference-worker processes each message in parallel, accumulates results in Redis with atomic DECR logic, and the last worker to finish assembles the final JSON. completion-worker parsesthe agrouped structure with no changes to its core logic.

**Tech Stack:** Python (asyncio, pika, redis), RabbitMQ, Redis, LLM (vLLM), GLiNER (already used)

---

## File Structure

| File | Responsibility |
|------|-----------------|
| `cmd/entities-worker/worker.py` | Fan-out logic: filter chunks → build entities_by_chunk → publish N messages + set counter |
| `cmd/inference-worker/worker.py` | Per-chunk processing: extract from message → LLM call → atomic RPUSH + DECR → optional assembly |
| `cmd/completion-worker/worker.py` | Parse grouped structure: iterate chunks → sum inferences |

No new files needed. Keep within existing worker architecture.

---

## Task 1: Update entities-worker to fan-out per chunk

**Files:**
- Modify: `cmd/entities-worker/worker.py:577-717`
- Test: existing `cmd/entities-worker/tests/test_api.py` (adapt if exists)

### Step 1: Understand current structure

Read lines 577–666 (chunk processing loop) and 694–717 (inference trigger).

Expected: Loop iterates chunks, accumulates `all_entities`. Current trigger: creates new RabbitMQ connection, publishes single message.

### Step 2: Build entities_by_chunk mapping during main loop

Add after line 657 (inside chunk loop, as each chunk is processed):

```python
# After all_entities.append(...) in the loop (line 648-657)
# Initialize tracking dict once
if not hasattr(self, '_entities_by_chunk'):
    self._entities_by_chunk = {}

chunk_id = chunk.get("chunk_id")
if chunk_id not in self._entities_by_chunk:
    self._entities_by_chunk[chunk_id] = []

# Add entities for this chunk (those with matching chunk_id in all_entities)
# We'll filter after the full loop
```

Actually, simpler: **build it at the end**, right before fan-out (line 694):

```python
# After all deduplication (line 682), before feature check (line 694)
entities_by_chunk = {}
for entity in all_entities:
    cid = entity.get("chunk_id")
    if cid not in entities_by_chunk:
        entities_by_chunk[cid] = []
    entities_by_chunk[cid].append(entity)
```

### Step 3: Modify inference trigger block (lines 694–717)

Replace the block with:

```python
# Check if micro-inferences are requested
try:
    features_json = self.redis_client.get(f"orchestrator:job:{job_id}:features")
    inferences_enabled = False
    if features_json:
        features = json.loads(features_json)
        inferences_enabled = "inferences" in features
    
    if inferences_enabled and chunks:
        # Build entities_by_chunk (filter to only chunks with text)
        entities_by_chunk = {}
        for entity in all_entities:
            cid = entity.get("chunk_id")
            if cid not in entities_by_chunk:
                entities_by_chunk[cid] = []
            entities_by_chunk[cid].append(entity)
        
        # Filter to chunks that have text (not empty)
        valid_chunks = [c for c in chunks if c.get("text", "").strip()]
        
        # Only proceed if there are chunks to process
        if valid_chunks:
            # Get source_type from Redis (set by source-classifier earlier)
            source_type_json = self.redis_client.get(
                f"orchestrator:job:{job_id}:source_classification"
            )
            source_type = "generico"  # default
            if source_type_json:
                try:
                    source_data = json.loads(source_type_json)
                    source_type = source_data.get("document_type", "generico")
                except:
                    pass
            
            # Set atomic counter for this job's inferences
            self.redis_client.setex(
                f"orchestrator:job:{job_id}:inferences:remaining",
                86400,  # TTL 24h
                len(valid_chunks)
            )
            
            # Publish one message per chunk with text
            params = parse_rabbitmq_url(RABBITMQ_URL)
            connection = pika.BlockingConnection(params)
            try:
                channel = connection.channel()
                for chunk in valid_chunks:
                    chunk_id = chunk.get("chunk_id")
                    chunk_text = chunk.get("text", "")
                    chunk_entities = entities_by_chunk.get(chunk_id, [])
                    
                    inference_msg = {
                        "job_id": job_id,
                        "chunk_id": chunk_id,
                        "chunk_text": chunk_text,
                        "entities": chunk_entities,
                        "source_type": source_type,
                        "total_chunks": len(valid_chunks)
                    }
                    
                    channel.basic_publish(
                        exchange="",
                        routing_key=INFERENCES_QUEUE,
                        body=json.dumps(inference_msg),
                        properties=pika.BasicProperties(delivery_mode=2),
                    )
                
                logger.info(
                    f"Published {len(valid_chunks)} inference tasks for job {job_id}"
                )
            finally:
                connection.close()
        
except Exception as e:
    logger.warning(f"Failed to trigger inferences: {e}")
    # Continue anyway - inference is optional
```

### Step 4: Run tests

```bash
cd /path/to/textflow
python3 -m py_compile cmd/entities-worker/worker.py
```

Expected: No syntax errors.

### Step 5: Commit

```bash
git add cmd/entities-worker/worker.py
git commit -m "feat: entities-worker fan-out per-chunk inference tasks

Instead of publishing single inference message per job, now publishes
one message per chunk (with chunk_id, text, and entities for that chunk).
Uses atomic Redis counter (SETEX) to track remaining chunks.

- Build entities_by_chunk mapping from all_entities
- Filter to chunks with text (valid_chunks)
- Publish N messages to inferences queue
- Set orchestrator:job:{id}:inferences:remaining counter"
```

---

## Task 2: Update inference-worker to process chunks with atomic assembly

**Files:**
- Modify: `cmd/inference-worker/worker.py:44-171`
- Test: `cmd/inference-worker/tests/test_chunking.py` (adapt if exists)

### Step 1: Rewrite extract_inferences() signature

Old: `extract_inferences(self, text: str, max_inferences: int = 5)`  
New: `extract_inferences(self, chunk_text: str, entities: List[Dict], source_type: str, max_inferences: int = 5)`

Replace lines 45–122 with:

```python
def extract_inferences(
    self,
    chunk_text: str,
    entities: List[Dict[str, Any]],
    source_type: str,
    max_inferences: int = 8,
) -> List[Dict[str, Any]]:
    """
    Extract micro-inferences from chunk text, guided by detected entities.
    
    Args:
        chunk_text: Text of the chunk (not truncated)
        entities: List of entities detected in this chunk
        source_type: Type of document source (notariado, catastro, etc)
        max_inferences: Maximum number of inferences to extract
        
    Returns:
        List of {"text": str, "confidence": float, "entities": [str]}
    """
    if not LLM_URL:
        logger.warning("No LLM_URL configured, skipping inferences")
        return []

    try:
        # Build entity reference string for prompt context
        entity_texts = [e.get("text", "") for e in entities]
        entities_str = ", ".join(entity_texts) if entity_texts else "(no entities detected)"
        
        prompt = f"""Dado el siguiente fragmento de texto y las entidades detectadas, extrae
todos los hechos concretos y verificables. Cada hecho debe mencionar al
menos una entidad detectada. Máximo {max_inferences} hechos.

Devuelve ÚNICAMENTE un array JSON con objetos que tengan:
- "text": la afirmación factual directa
- "confidence": valor entre 0.0 y 1.0
- "entities": lista de nombres de entidades mencionadas en el hecho

Entidades detectadas: {entities_str}

Fragmento de texto:
{chunk_text}

Hechos:"""

        payload = {
            "model": LLM_MODEL,
            "prompt": prompt,
            "max_tokens": 500,
            "temperature": 0.1,
        }
        
        response = requests.post(
            f"{LLM_URL}/v1/completions",
            json=payload,
            timeout=30,
        )
        response.raise_for_status()
        
        result = response.json()
        completion_text = result.get("choices", [{}])[0].get("text", "")
        
        # Parse JSON from LLM response
        json_match = re.search(r"\[.*\]", completion_text, re.DOTALL)
        if not json_match:
            logger.warning("No JSON found in LLM response")
            return []
        
        inferences = json.loads(json_match.group())
        
        # Validate and annotate
        validated = []
        for inf in inferences:
            if isinstance(inf, dict) and "text" in inf:
                validated.append({
                    "text": inf.get("text", ""),
                    "confidence": float(inf.get("confidence", 0.5)),
                    "entities": inf.get("entities", []),
                })
        
        logger.info(f"Extracted {len(validated)} inferences from chunk")
        return validated
        
    except requests.RequestException as e:
        logger.warning(f"LLM call failed: {e}")
        return []
    except json.JSONDecodeError as e:
        logger.warning(f"Failed to parse LLM response: {e}")
        return []
    except Exception as e:
        logger.error(f"Error extracting inferences: {e}")
        return []
```

### Step 2: Rewrite process() method to handle new message format and assembly logic

Replace lines 124–170 with:

```python
def process(self, ch, method, properties, body):
    start_time = time.time()
    job_id = None
    chunk_id = None

    try:
        message = json.loads(body)
        job_id = message.get("job_id")
        chunk_id = message.get("chunk_id")
        chunk_text = message.get("chunk_text", "")
        entities = message.get("entities", [])
        source_type = message.get("source_type", "generico")
        total_chunks = message.get("total_chunks", 1)

        logger.info(f"Processing inferences for job: {job_id}, chunk: {chunk_id}")

        if not chunk_text:
            logger.warning(f"No text in message for job: {job_id}, chunk: {chunk_id}")
            jobs_total.labels(status="no_text").inc()
            ch.basic_nack(delivery_tag=method.delivery_tag, requeue=False)
            return

        # Extract inferences with entity context (no truncation)
        inferences = self.extract_inferences(
            chunk_text=chunk_text,
            entities=entities,
            source_type=source_type
        )

        # Build result for this chunk
        chunk_result = {
            "chunk_id": chunk_id,
            "inferences": inferences,
        }

        # Append to Redis list (intermediate storage)
        inferences_raw_key = f"orchestrator:job:{job_id}:micro_inferences_raw"
        self.redis_client.rpush(inferences_raw_key, json.dumps(chunk_result))

        # Decrement atomic counter
        remaining_key = f"orchestrator:job:{job_id}:inferences:remaining"
        remaining = self.redis_client.decr(remaining_key)

        logger.info(
            f"Inference completed for job: {job_id}, chunk: {chunk_id}, "
            f"inferences: {len(inferences)}, remaining chunks: {remaining}"
        )

        # If this is the last chunk (remaining <= 0), assemble final result
        if remaining <= 0:
            logger.info(f"All inferences complete for job {job_id}, assembling results...")
            
            try:
                # Get all intermediate results
                raw_results = self.redis_client.lrange(inferences_raw_key, 0, -1)
                
                # Parse and assemble
                assembled = []
                for raw_json in raw_results:
                    try:
                        chunk_data = json.loads(raw_json)
                        assembled.append(chunk_data)
                    except json.JSONDecodeError as e:
                        logger.warning(f"Failed to parse intermediate result: {e}")
                        continue
                
                # Store final result
                final_key = f"orchestrator:job:{job_id}:micro_inferences"
                self.redis_client.set(final_key, json.dumps(assembled))
                
                # Clean up intermediate keys
                self.redis_client.delete(inferences_raw_key)
                self.redis_client.delete(remaining_key)
                
                # Mark step as completed
                self.redis_client.hset(
                    f"orchestrator:job:{job_id}:steps", "inferences", "completed"
                )
                
                # Publish progress
                self.event_bus.publish_job_progress(job_id, 80, "inferences")
                
                logger.info(
                    f"Inferences finalized for job: {job_id}, "
                    f"total chunks: {len(assembled)}, "
                    f"total inferences: {sum(len(c['inferences']) for c in assembled)}"
                )
                
                jobs_total.labels(status="success").inc()
                
            except Exception as e:
                logger.error(f"Error assembling final inferences: {e}")
                # Mark as failed
                self.redis_client.hset(
                    f"orchestrator:job:{job_id}:steps", "inferences", "failed"
                )
                jobs_total.labels(status="assembly_error").inc()
        else:
            # Not the last chunk, just continue
            jobs_total.labels(status="chunk_processed").inc()

        duration = time.time() - start_time
        job_duration.observe(duration)

        ch.basic_ack(delivery_tag=method.delivery_tag)

    except Exception as e:
        logger.error(f"Error processing inferences: {e}")
        jobs_total.labels(status="error").inc()
        ch.basic_nack(delivery_tag=method.delivery_tag, requeue=True)
```

### Step 3: Verify imports are at top

Check that `re` is imported at module level (line 13, added in earlier audit).
It should already be there from the audit fix. If not, add:

```python
import re  # at line 13, with other imports
```

### Step 4: Run syntax check

```bash
python3 -m py_compile cmd/inference-worker/worker.py
```

Expected: No syntax errors.

### Step 5: Commit

```bash
git add cmd/inference-worker/worker.py
git commit -m "feat: inference-worker per-chunk processing with atomic assembly

Process one message per chunk (not full document). Extract inferences
with entity context in prompt (no truncation). Use atomic Redis DECR
to detect when last chunk is processed; assemble final result in that
worker.

Key changes:
- extract_inferences() now takes chunk_text, entities, source_type
- process() uses new message format {job_id, chunk_id, chunk_text, entities}
- RPUSH intermediate results to micro_inferences_raw
- DECR counter to detect last worker
- Final worker assembles from LRANGE and publishes to micro_inferences
- Cleans up intermediate keys (micro_inferences_raw, inferences:remaining)"
```

---

## Task 3: Update completion-worker to parse grouped inferences

**Files:**
- Modify: `cmd/completion-worker/worker.py:290-301`
- Test: existing tests

### Step 1: Understand current parsing

Read lines 268–272 (try/except for micro_inferences_json parsing).

Current: Assumes `micro_inferences` is a dict and calls `.get("inferences", [])`.

New: It's a list of `{chunk_id, inferences: [...]}`, so iterate to count.

### Step 2: Replace micro_inferences parsing block

Replace lines 268–272:

```python
try:
    if micro_inferences_json:
        micro_inferences = json.loads(micro_inferences_json)
except json.JSONDecodeError as e:
    logger.warning(f"Failed to parse micro_inferences JSON: {e}")
```

With:

```python
try:
    if micro_inferences_json:
        micro_inferences = json.loads(micro_inferences_json)
        # micro_inferences is now: [{chunk_id: "...", inferences: [...]}, ...]
        # Validate structure
        if not isinstance(micro_inferences, list):
            logger.warning(f"micro_inferences is not a list, got {type(micro_inferences)}")
            micro_inferences = None
except json.JSONDecodeError as e:
    logger.warning(f"Failed to parse micro_inferences JSON: {e}")
    micro_inferences = None
```

### Step 3: Update logging in finalize_job

Replace lines 293–301:

```python
# Log completion stats
log_message = f"Job {job_id} finalized: chunks={len(chunks)}, entities={len(entities)}"
if source_classification:
    log_message += f", source_type={source_classification.get('document_type', 'unknown')}"
if micro_inferences:
    # micro_inferences is a list, so len() directly works
    inferences_count = len(micro_inferences) if isinstance(micro_inferences, list) else 0
    log_message += f", inferences={inferences_count}"
logger.info(log_message)
```

With:

```python
# Log completion stats
log_message = f"Job {job_id} finalized: chunks={len(chunks)}, entities={len(entities)}"
if source_classification:
    log_message += f", source_type={source_classification.get('document_type', 'unknown')}"
if micro_inferences:
    # micro_inferences is now a list of {chunk_id, inferences: [...]}
    if isinstance(micro_inferences, list):
        total_inferences = sum(len(c.get("inferences", [])) for c in micro_inferences)
        log_message += f", micro_inferences={total_inferences} (from {len(micro_inferences)} chunks)"
    else:
        log_message += f", micro_inferences=<invalid structure>"
logger.info(log_message)
```

### Step 4: Verify results dict includes grouped inferences

Check lines 288–291 (where micro_inferences is added to results dict).

Current code should already store it correctly:
```python
if micro_inferences is not None:
    results["micro_inferences"] = micro_inferences
```

This is fine — it will now store the grouped list instead of a dict. No change needed here.

### Step 5: Run syntax check

```bash
python3 -m py_compile cmd/completion-worker/worker.py
```

Expected: No syntax errors.

### Step 6: Commit

```bash
git add cmd/completion-worker/worker.py
git commit -m "feat: completion-worker parse grouped micro-inferences

micro_inferences is now a list grouped by chunk_id:
  [{chunk_id: '...', inferences: [...]}, ...]

Update parsing to handle list structure. Count total inferences by
summing across all chunks. Store grouped structure in final results.

No changes to results dict key or format — just the value structure."
```

---

## Task 4: Verify the flow with integration sanity checks

**Files:**
- Test: `cmd/entities-worker/tests/`, `cmd/inference-worker/tests/`, `cmd/completion-worker/`
- Reference: spec document

### Step 1: Syntax check all three workers

```bash
python3 -m py_compile cmd/entities-worker/worker.py
python3 -m py_compile cmd/inference-worker/worker.py
python3 -m py_compile cmd/completion-worker/worker.py
```

Expected: All pass with no syntax errors.

### Step 2: Check for any imports needed

Verify all imports are present at the top of each file:
- `entities-worker`: pika, redis, json, logging
- `inference-worker`: re, redis, pika, requests, json
- `completion-worker`: json, redis

All should already be there.

### Step 3: Docker compose validation (optional, if infra available)

```bash
MODELS_PATH=/models docker compose -f deploy/docker/docker-compose.yml config --quiet
```

Expected: No errors (validate YAML syntax).

### Step 4: Create integration test outline (documentation, not code)

Create file: `docs/integration/micro-inferences-per-chunk-flow.md`

```markdown
# Micro-Inferences Per-Chunk Integration Test Flow

## Prerequisites
- RabbitMQ running
- Redis running
- LLM (vLLM) running on LLM_URL
- entities-worker, inference-worker, completion-worker running

## Test Scenario: 3-chunk document with inferences feature

1. **Orchestrator publishes job message:**
   - job_id: test-123
   - chunks: 3 chunks with text
   - features: ["inferences"]

2. **entities-worker processes:**
   - Extracts entities from all chunks
   - Publishes 3 messages to "inferences" queue
   - SETEX inferences:remaining = 3

3. **inference-worker #1 processes chunk-0:**
   - Gets message with chunk_id, chunk_text, entities
   - Calls LLM
   - RPUSH to micro_inferences_raw
   - DECR → remaining=2

4. **inference-worker #2 processes chunk-1:**
   - Similar → remaining=1

5. **inference-worker #3 processes chunk-2:**
   - Similar → remaining=0
   - Detects remaining <= 0
   - LRANGE micro_inferences_raw → assembles [{chunk_id, inferences}, ...]
   - SET micro_inferences = final_json
   - HSET steps inferences completed

6. **completion-worker listens on job_progress:**
   - Detects inferences step completed
   - Checks all required steps done
   - Calls finalize_job()
   - Parses micro_inferences as grouped list
   - Includes in final results

## Expected Outcome
- micro_inferences in final results: list of {chunk_id, inferences: [...]}
- No truncation: full chunk text processed
- Entities visible in final inferences list
```

### Step 5: Commit

```bash
git add docs/integration/micro-inferences-per-chunk-flow.md
git commit -m "docs: add integration test flow for per-chunk inferences

Documents the end-to-end flow from entities-worker fan-out through
inference-worker parallel processing to completion-worker assembly.

Serves as reference for manual testing and future test automation."
```

---

## Task 5: Final cleanup and validation

**Files:**
- Check: all modified workers

### Step 1: Run full diff to check for syntax and logic

```bash
git diff HEAD~3 HEAD cmd/entities-worker/ cmd/inference-worker/ cmd/completion-worker/
```

Expected: Changes are clear and localized. No unintended modifications.

### Step 2: Ensure all commits are clean

```bash
git log --oneline -5
```

Expected:
```
aXXXXXX docs: add integration test flow for per-chunk inferences
aXXXXXX feat: completion-worker parse grouped micro-inferences
aXXXXXX feat: inference-worker per-chunk processing with atomic assembly
aXXXXXX feat: entities-worker fan-out per-chunk inference tasks
...
```

### Step 3: Verify no uncommitted changes

```bash
git status
```

Expected: "working tree clean" or only untracked docs/ (if generated).

### Step 4: Summary output

Print summary:

```
✅ Per-Chunk Micro-Inferences Implementation Complete

Modified Files:
- cmd/entities-worker/worker.py (fan-out logic)
- cmd/inference-worker/worker.py (per-chunk processing + assembly)
- cmd/completion-worker/worker.py (grouped parsing)

New Documentation:
- docs/superpowers/specs/2026-03-26-micro-inferences-per-chunk-design.md
- docs/superpowers/plans/2026-03-26-micro-inferences-per-chunk-implementation.md
- docs/integration/micro-inferences-per-chunk-flow.md

Key Changes:
- Fan-out: N messages per job (one per chunk)
- Atomic Assembly: DECR counter + LRANGE + SET final result
- No Truncation: Full chunk boundaries (no 2000-char limit)
- Entity Context: Prompt includes detected entities
- Grouped Output: [{chunk_id, inferences: [...]}, ...]

Ready for: testing, integration, and deployment
```

### Step 5: Final commit (summary)

No commit needed — implementation is complete with 4 task commits.

---

## Rollback Instructions (if needed)

If any issue found during testing:

```bash
# Revert last 4 commits
git reset --hard HEAD~4

# Or cherry-pick specific fixes
git revert <commit-hash>
```

---

## Next Steps After Implementation

1. **Testing**: Run against small test job (3 chunks) to verify counter logic
2. **Performance Testing**: Benchmark with 20-50 chunk document
3. **Integration Testing**: Full pipeline with orchestrator → entities → inferences → completion
4. **Deployment**: Follow project's CI/CD pipeline
