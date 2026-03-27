# Dynamic Model Discovery & Adaptive Timeout Implementation Plan

> **For agentic workers:** Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the inference worker dynamically discover available LLM models from vLLM API instead of hardcoding them, calculate max_tokens based on actual max_model_len, fix the 5m job timeout that kills long documents, and optimize GPU memory usage for inference on larger documents.

**Architecture:** 
- Inference-worker queries `/v1/models` at startup and caches model metadata (id, max_model_len)
- Calculates `max_tokens` request parameter dynamically based on discovered max_model_len
- Falls back gracefully if vLLM is unreachable (warns, continues without inferences)
- Job timeout increased from 5m → 60m via environment variable (not hardcoded)
- vLLM context window reduced 16384 → 2048 to free ~4GB VRAM for GPU workers

**Tech Stack:** Python 3.11, vLLM API, Redis, RabbitMQ, pytest

---

## Task 1: Add Model Discovery to Inference Worker

**Files:**
- Modify: `cmd/inference-worker/worker.py:32-33, 40-44, 70-190`
- Test: `cmd/inference-worker/tests/test_inference_worker.py`

- [ ] **Step 1: Read current inference-worker code and tests**

Already done — we have the code in memory.

- [ ] **Step 2: Add `_discover_model()` static method to InferenceWorker class**

This method:
- Makes GET request to `{LLM_URL}/v1/models`
- Parses the response to extract `data[0]["id"]` and `data[0]["max_model_len"]`
- Returns `(model_id: str, max_model_len: int)` tuple
- Logs discovery or returns `(None, None)` if unreachable (graceful fallback)

```python
@staticmethod
def _discover_model(llm_url: str) -> tuple[Optional[str], Optional[int]]:
    """
    Discover available models from vLLM API.
    
    Args:
        llm_url: Base URL of vLLM server (e.g., http://localhost:8000)
        
    Returns:
        Tuple of (model_id, max_model_len) or (None, None) if discovery fails
    """
    if not llm_url:
        logger.warning("No LLM_URL configured, model discovery skipped")
        return (None, None)
    
    try:
        response = requests.get(
            f"{llm_url}/v1/models",
            timeout=5,
        )
        response.raise_for_status()
        models = response.json()
        
        if not models.get("data"):
            logger.warning(f"No models found in vLLM response: {models}")
            return (None, None)
        
        model_info = models["data"][0]
        model_id = model_info.get("id")
        max_model_len = model_info.get("max_model_len", 4096)
        
        logger.info(
            f"Discovered model '{model_id}' with max_model_len={max_model_len}"
        )
        return (model_id, max_model_len)
        
    except requests.RequestException as e:
        logger.warning(f"Failed to discover models from {llm_url}: {e}")
        return (None, None)
    except (KeyError, ValueError) as e:
        logger.warning(f"Failed to parse vLLM models response: {e}")
        return (None, None)
```

- [ ] **Step 3: Update `__init__` to cache discovered model info**

Modify the `__init__` method to:
- Call `_discover_model(LLM_URL)` once at startup
- Store `self.llm_model_id` and `self.llm_max_model_len` as instance variables
- If discovery fails, `self.llm_model_id = None` (graceful degradation)

```python
def __init__(self):
    self.redis_client = redis.from_url(REDIS_URL, decode_responses=True)
    self.event_bus = EventBus(self.redis_client)
    
    # Discover model at startup (once, cached)
    if LLM_URL:
        self.llm_model_id, self.llm_max_model_len = self._discover_model(LLM_URL)
    else:
        self.llm_model_id = None
        self.llm_max_model_len = None
```

- [ ] **Step 4: Update `extract_inferences()` to use discovered model and dynamic max_tokens**

Modify the method to:
- Use `self.llm_model_id` (discovered) instead of `LLM_MODEL` (hardcoded)
- Calculate `max_tokens` dynamically: `max(200, self.llm_max_model_len - 900)` where 900 is overhead estimate for prompt + chunk
- If `self.llm_model_id` is None, return `[]` early (graceful fallback)

```python
def extract_inferences(
    self,
    chunk_text: str,
    entities: List[Dict[str, Any]],
    source_type: str,
    max_inferences: int = 8,
) -> List[Dict[str, Any]]:
    """..."""
    if not LLM_URL or not self.llm_model_id:
        logger.warning("No LLM configured or model discovery failed, skipping inferences")
        return []
    
    try:
        entity_texts = [e.get("text", "") for e in entities]
        entities_str = ", ".join(entity_texts) if entity_texts else "(no entities detected)"
        
        # ... prompts unchanged ...
        
        # Calculate max_tokens dynamically from discovered max_model_len
        # Estimate: overhead = system_prompt (150) + user_prompt (300) + margin (450)
        max_tokens = max(200, (self.llm_max_model_len or 4096) - 900)
        
        payload = {
            "model": self.llm_model_id,  # Use discovered model
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "max_tokens": max_tokens,  # Dynamic, not hardcoded 500
            "temperature": 0.1,
            "chat_template_kwargs": {"enable_thinking": False},
        }
        
        # ... rest unchanged ...
```

- [ ] **Step 5: Update environment variable handling**

Modify module-level constants to remove default for `LLM_MODEL`:

```python
LLM_URL = os.getenv("LLM_URL", "")  # Base URL without /v1 path
LLM_MODEL = os.getenv("LLM_MODEL", "")  # Will be auto-discovered, left empty by default
```

- [ ] **Step 6: Write test for model discovery**

Add test to `cmd/inference-worker/tests/test_inference_worker.py`:

```python
def test_discover_model_success(self):
    """Test successful model discovery from /v1/models endpoint"""
    with patch("requests.get") as mock_get:
        mock_response = Mock()
        mock_response.json.return_value = {
            "data": [{
                "id": "qwen3.5-2b",
                "max_model_len": 16384,
            }]
        }
        mock_get.return_value = mock_response
        
        model_id, max_len = InferenceWorker._discover_model("http://localhost:8000")
        
        assert model_id == "qwen3.5-2b"
        assert max_len == 16384
        mock_get.assert_called_once_with(
            "http://localhost:8000/v1/models",
            timeout=5,
        )

def test_discover_model_unreachable(self):
    """Test graceful fallback when vLLM is unreachable"""
    with patch("requests.get") as mock_get:
        mock_get.side_effect = requests.ConnectionError("Connection refused")
        
        model_id, max_len = InferenceWorker._discover_model("http://localhost:8000")
        
        assert model_id is None
        assert max_len is None

def test_extract_inferences_uses_discovered_model(self, worker):
    """Test that extract_inferences uses discovered model and dynamic max_tokens"""
    worker.llm_model_id = "qwen3.5-2b"
    worker.llm_max_model_len = 2048
    
    with patch("worker.LLM_URL", "http://localhost:8000"):
        with patch("requests.post") as mock_post:
            mock_response = Mock()
            mock_response.raise_for_status = Mock()
            mock_response.json.return_value = {
                "choices": [{
                    "message": {
                        "content": '[{"text": "Test fact", "confidence": 0.95, "entities": []}]'
                    }
                }]
            }
            mock_post.return_value = mock_response
            
            inferences = worker.extract_inferences(
                chunk_text="Some text",
                entities=[],
                source_type="catastro",
            )
            
            # Verify POST payload uses discovered model
            call_kwargs = mock_post.call_args[1]
            assert call_kwargs["json"]["model"] == "qwen3.5-2b"
            # max_tokens should be max(200, 2048-900) = 1148
            assert call_kwargs["json"]["max_tokens"] == 1148
```

- [ ] **Step 7: Run tests to verify implementation**

```bash
cd /path/to/textflow
pytest cmd/inference-worker/tests/test_inference_worker.py -v
```

Expected: All 7 tests pass (4 existing + 3 new discovery tests)

- [ ] **Step 8: Commit**

```bash
git add cmd/inference-worker/worker.py cmd/inference-worker/tests/test_inference_worker.py
git commit -m "feat: implement dynamic LLM model discovery from vLLM /v1/models API

- Add _discover_model() method to query vLLM for available models at startup
- Cache model_id and max_model_len in instance variables
- Calculate max_tokens dynamically based on discovered context window
- Graceful fallback: if vLLM unreachable, continue without inferences
- Remove hardcoded LLM_MODEL default (now auto-discovered)
- Add comprehensive tests for discovery success/failure scenarios
- Enables system to adapt to any vLLM configuration without code changes"
```

---

## Task 2: Increase Job Timeout (Remove Hardcoding)

**Files:**
- Modify: `deploy/docker/docker-compose.yml:80`
- Modify: `dist/config/docker-compose.yml:79`
- Modify: `.env`

- [ ] **Step 1: Update docker-compose.yml files to use environment variable**

Change from:
```yaml
- JOB_TIMEOUT=5m
```

To:
```yaml
- JOB_TIMEOUT=${JOB_TIMEOUT:-60m}
```

This allows:
- Override via environment variable: `JOB_TIMEOUT=120m docker compose up`
- Default to 60m if not set in environment

- [ ] **Step 2: Update .env to set default JOB_TIMEOUT**

Add to `.env`:
```
JOB_TIMEOUT=60m
```

- [ ] **Step 3: Verify docker-compose interpolation works**

```bash
cd /path/to/textflow
grep "JOB_TIMEOUT" deploy/docker/docker-compose.yml dist/config/docker-compose.yml
grep "JOB_TIMEOUT" .env
```

Expected: Both files show `JOB_TIMEOUT=${JOB_TIMEOUT:-60m}` and `.env` shows `JOB_TIMEOUT=60m`

- [ ] **Step 4: Commit**

```bash
git add deploy/docker/docker-compose.yml dist/config/docker-compose.yml .env
git commit -m "fix: make JOB_TIMEOUT configurable instead of hardcoded 5m

- Change JOB_TIMEOUT from hardcoded '5m' to '\${JOB_TIMEOUT:-60m}' in both docker-compose files
- Add JOB_TIMEOUT=60m to .env as default
- Allows override: JOB_TIMEOUT=120m docker compose up
- Fixes issue where 136-chunk documents timed out before workers could finish
- 60m provides sufficient time for all processing steps on large documents"
```

---

## Task 3: Optimize vLLM VRAM Usage (Free ~4GB for GPU Workers)

**Files:**
- Modify: `/path/to/local-vllm/.env`

- [ ] **Step 1: Understand current vLLM memory breakdown**

Current state:
- max_model_len=16384 (Qwen3.5-2B context window)
- Our chunks: 512 tokens max
- Full request: prompt (~150-300t) + chunk (512t) + response (~100t) = ~700-900t ≈ OK with 2048
- KV cache with 16384: ~4.6GB; with 2048: ~0.6GB

- [ ] **Step 2: Update local-vllm/.env**

Change:
```
VLLM_MAX_MODEL_LEN_2B=16384
VLLM_GPU_MEMORY_UTILIZATION_2B=0.70
```

To:
```
VLLM_MAX_MODEL_LEN_2B=2048
VLLM_GPU_MEMORY_UTILIZATION_2B=0.45
```

Rationale:
- 2048 is sufficient for our use case (chunks 512t + prompt + response)
- Frees ~4GB KV cache memory
- Reduces gpu_memory_utilization to avoid OOM: 0.70 × 12GB = 8.4GB (leaves no room for workers)
- 0.45 × 12GB = 5.4GB → model + KV cache fit, leaves room for embeddings/entities GPU inference

- [ ] **Step 3: Verify changes**

```bash
cat /path/to/local-vllm/.env | grep -A 2 "VLLM_MAX_MODEL_LEN_2B\|VLLM_GPU_MEMORY_UTILIZATION_2B"
```

Expected:
```
VLLM_MAX_MODEL_LEN_2B=2048
VLLM_GPU_MEMORY_UTILIZATION_2B=0.45
```

- [ ] **Step 4: Commit**

```bash
git add /path/to/local-vllm/.env
git commit -m "fix: reduce vLLM context window to free VRAM for GPU workers

- Set VLLM_MAX_MODEL_LEN_2B=2048 (was 16384)
  - Chunks are 512t; with prompt+response ~900t total, 2048 is ample margin
  - Frees ~4GB of KV cache VRAM
- Set VLLM_GPU_MEMORY_UTILIZATION_2B=0.45 (was 0.70)
  - Prevents OOM when running concurrent GPU inference workers
  - New memory layout: vLLM 5.4GB + embeddings/entities 1.5GB+1GB = ~8.9GB
- Enables moving embeddings-worker (bge-m3) and entities-worker (GLiNER) to GPU
- Expected performance: ~20min → ~1-2min for 136 chunks with GPU workers"
```

---

## Task 4: Restart Containers with GPU and New Config

**Files:** None (runtime operations)

- [ ] **Step 1: Restart vLLM with new context window**

```bash
cd /path/to/local-vllm
docker compose up -d --force-recreate vllm-2b
# Wait for health check to pass
docker compose logs -f vllm-2b 2>&1 | head -50
```

Expected output: vLLM starts with `--max-model-len 2048 --gpu-memory-utilization 0.45`

Wait until: `Uvicorn running on http://0.0.0.0:8000` and health check returns OK (green)

- [ ] **Step 2: Restart orchestrator to pick up new JOB_TIMEOUT**

```bash
cd /path/to/textflow
docker compose restart orchestrator
docker compose logs orchestrator 2>&1 | head -20
```

Expected: Orchestrator starts with new timeout value

- [ ] **Step 3: Restart embeddings and entities workers with GPU overlay**

```bash
cd /path/to/textflow
docker compose -f deploy/docker/docker-compose.yml -f deploy/docker/docker-compose.gpu.yml up -d --force-recreate embeddings-worker entities-worker
# Wait for startup
sleep 10
docker compose logs embeddings-worker entities-worker 2>&1 | grep -E "CUDA|cuda|GPU|GPU" | head -10
```

Expected:
- embeddings-worker: `EMBEDDINGS_DEVICE=cuda` and CUDA initialization logs
- entities-worker: `ENTITIES_DEVICE=cuda` and GLiNER CUDA logs

- [ ] **Step 4: Verify GPU memory is now reasonable**

```bash
nvidia-smi
```

Expected (rough numbers):
- vLLM: ~5.4GB (was ~8.6GB)
- docling: ~3.1GB
- embeddings-worker + entities-worker: available on GPU for concurrent inference
- Total: ~8.5GB (leaving ~3.8GB for OS/buffers)

- [ ] **Step 5: Test model discovery is working**

Check inference-worker logs:

```bash
docker compose logs inference-worker 2>&1 | grep -i "discover\|model"
```

Expected: `Discovered model 'qwen3.5-2b' with max_model_len=2048`

---

## Task 5: End-to-End Test with Servo Drive Manual

**Files:** None (testing only)

- [ ] **Step 1: Upload and test with AASD_servodrivemanual.pdf**

```bash
cd /path/to/textflow
tools/client/client -i /path/to/samples/AASD_servodrivemanual.pdf \
  -o /tmp/servo_final.json \
  -u http://localhost:8080 \
  --inferences
```

- [ ] **Step 2: Monitor job progress**

Expected progression:
- Job created with ID
- Extraction: 2-5 seconds (docling PDF → text)
- Embeddings (GPU): 136 chunks @ ~0.3s/chunk ≈ 40 seconds total
- Entities (GPU): 136 chunks @ ~0.5-1s/chunk ≈ 1-2 minutes total
- Inferences (vLLM): 136 chunks @ ~3s/chunk ≈ 6-7 minutes total
- **Total expected: ~8-10 minutes** (was ~300 seconds before timeout)

- [ ] **Step 3: Verify job completed successfully**

```bash
# Wait for job to finish
while true; do
  status=$(curl -s http://localhost:8080/health | jq '.status' 2>/dev/null || echo "pending")
  echo "$(date): Status = $status"
  [[ "$status" == "success" ]] && break
  sleep 10
done

# Check output file
jq '.micro_inferences | length' /tmp/servo_final.json
```

Expected:
- Job status: `completed`
- micro_inferences: array with 136 objects (one per chunk)
- Each object: `{"chunk_id": N, "inferences": [...]}`

- [ ] **Step 4: Spot-check inference quality**

```bash
jq '.micro_inferences[0:3] | .[] | {chunk_id, inferences_count: (.inferences | length)}' /tmp/servo_final.json
```

Expected: Several inferences per chunk, reasonable quality

- [ ] **Step 5: Document results**

Create `/tmp/TEST_RESULTS_FINAL.txt`:
```
=== E2E Test: AASD_servodrivemanual.pdf with GPU Optimization ===

Job ID: <from output>
Total chunks: 136
Total inferences extracted: <count>

Timeline:
- Extraction: <time>
- Embeddings (GPU): <time>
- Entities (GPU): <time>
- Inferences (vLLM): <time>
- Total: <time>

GPU Memory (during execution):
- vLLM: ~5.4GB
- docling: ~3.1GB
- workers: <utilized>

Status: ✅ PASSED
```

---

## Success Criteria

✅ All tests pass: `pytest cmd/inference-worker/tests/test_inference_worker.py -v`
✅ Model discovery logged on inference-worker startup
✅ Job timeout no longer kills large documents at 5m
✅ GPU memory utilization reasonable: vLLM + docling + workers fit in 12GB
✅ E2E test completes in <15 min (was failing at 5m before)
✅ micro_inferences present in final output for all 136 chunks

