# Inference Pipeline & Source Classification Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add source classification (document type detection) and optional micro-inference extraction (LLM-based fact extraction) to the pipeline, with results returned in final job JSON alongside existing embeddings/entities.

**Architecture:** 
- **Source classification** runs inline in extraction-worker (regex patterns, instant, always runs)
- **Micro-inferences** run in new inference-worker (LLM calls, opt-in via `features` flag)
- Go orchestrator stores `features` and `llm_url` in Redis at job creation
- Entities-worker checks Redis for features, triggers inference queue if needed
- Completion-worker aggregates final results including new data
- No RabbitMQ message format changes needed — workers read Redis directly

**Tech Stack:** 
- Go (config, Redis methods, orchestrator handlers)
- Python (extraction-worker, entities-worker, new inference-worker)
- RabbitMQ (new `inferences` queue)
- Redis (feature flags, llm_url storage)

---

## File Structure

### Files to Create
- `cmd/inference-worker/worker.py` — Main inference service (LLM calls, result aggregation)
- `cmd/inference-worker/requirements.txt` — Dependencies (requests, redis, pika, etc.)
- `cmd/inference-worker/Dockerfile` — Container build
- `cmd/inference-worker/tests/test_inference_worker.py` — Unit tests

### Files to Modify
- `internal/models/job.go` — Add Go types for features, llm_url, MicroInference
- `internal/config/config.go` — Add InferencesQueue config
- `internal/broker/rabbitmq.go` — Declare inferences queue
- `internal/redis/client.go` — Add Redis methods for features/llm_url/inferences, update DeleteJob
- `cmd/orchestrator/main.go` — Parse features + llm_url from requests, store in Redis
- `cmd/extraction-worker/worker.py` — Add SourceClassifier class, inline classification
- `cmd/entities-worker/worker.py` — Check features in Redis, publish to inferences queue
- `cmd/completion-worker/worker.py` — Read inferences from Redis, include in results, adjust required_steps
- `deploy/docker/docker-compose.yml` — Add inference-worker service
- `cmd/extraction-worker/requirements.txt` — (may need to add re if not present, but it's stdlib)

---

## Task 1: Go Model Types & Redis Methods

**Files:**
- Modify: `internal/models/job.go:31-42`
- Modify: `internal/redis/client.go:324-341` (DeleteJob)
- Create: New Redis methods for features/llm_url/inferences

### Step 1.1: Add Go types to models/job.go

Add after `JobResults` struct (around line 42):

```go
type SourceClassificationResult struct {
	DocumentType string  `json:"document_type"` // e.g. "notariado", "catastro", "bancario"
	Confidence   float32 `json:"confidence"`
	ClassifierVersion string `json:"classifier_version"`
}

type MicroInference struct {
	Fact       string  `json:"fact"`           // e.g. "The property value is 500,000 EUR"
	Confidence float32 `json:"confidence"`
	Source     string  `json:"source"`         // e.g. "extraction" or "llm"
}

// Update JobResults to include new fields
// (will modify this in step 1.3)
```

### Step 1.2: Update JobResults struct to include inferences & classification

In `internal/models/job.go`, update `JobResults` struct (line 31):

```go
type JobResults struct {
	JobID                  string                 `json:"job_id"`
	Status                 string                 `json:"status"`
	CreatedAt              string                 `json:"created_at"`
	CompletedAt            string                 `json:"completed_at"`
	Text                   string                 `json:"text"`
	Chunks                 []Chunk                `json:"chunks,omitempty"`
	Embeddings             map[string]interface{} `json:"embeddings,omitempty"`
	Entities               []Entity               `json:"entities,omitempty"`
	DocumentMetadata       map[string]interface{} `json:"document_metadata,omitempty"`
	TextMetadata           map[string]interface{} `json:"text_metadata,omitempty"`
	SourceClassification   *SourceClassificationResult `json:"source_classification,omitempty"`
	MicroInferences        []MicroInference       `json:"micro_inferences,omitempty"`
}
```

### Step 1.3: Add Redis methods to internal/redis/client.go

After `SetJobStatus` method (around line 94), add:

```go
func (c *RedisClient) SetJobFeatures(ctx context.Context, jobID string, features []string) error {
	key := c.key("job", jobID, "features")
	data, err := json.Marshal(features)
	if err != nil {
		return fmt.Errorf("failed to marshal features: %w", err)
	}
	err = c.client.Set(ctx, key, data, c.jobTTL).Err()
	if err != nil {
		return fmt.Errorf("failed to set job features: %w", err)
	}
	return nil
}

func (c *RedisClient) GetJobFeatures(ctx context.Context, jobID string) ([]string, error) {
	key := c.key("job", jobID, "features")
	data, err := c.client.Get(ctx, key).Result()
	if err != nil {
		if err == redis.Nil {
			return []string{}, nil
		}
		return nil, fmt.Errorf("failed to get job features: %w", err)
	}
	var features []string
	if err := json.Unmarshal([]byte(data), &features); err != nil {
		return nil, fmt.Errorf("failed to unmarshal features: %w", err)
	}
	return features, nil
}

func (c *RedisClient) SetJobLLMURL(ctx context.Context, jobID string, llmURL string) error {
	key := c.key("job", jobID, "llm_url")
	err := c.client.Set(ctx, key, llmURL, c.jobTTL).Err()
	if err != nil {
		return fmt.Errorf("failed to set job llm_url: %w", err)
	}
	return nil
}

func (c *RedisClient) GetJobLLMURL(ctx context.Context, jobID string) (string, error) {
	key := c.key("job", jobID, "llm_url")
	url, err := c.client.Get(ctx, key).Result()
	if err != nil {
		if err == redis.Nil {
			return "", nil
		}
		return "", fmt.Errorf("failed to get job llm_url: %w", err)
	}
	return url, nil
}
```

### Step 1.4: Update DeleteJob to include new keys

In `internal/redis/client.go` at line 324, update the `DeleteJob` method keys list:

```go
func (c *RedisClient) DeleteJob(ctx context.Context, jobID string) error {
	keys := []string{
		c.key("job", jobID, "status"),
		c.key("job", jobID, "text"),
		c.key("job", jobID, "results"),
		c.key("job", jobID, "embeddings"),
		c.key("job", jobID, "entities"),
		c.key("job", jobID, "entities_raw"),
		c.key("job", jobID, "metadata"),
		c.key("job", jobID, "steps"),
		c.key("job", jobID, "meta"),
		c.key("job", jobID, "error"),
		c.key("job", jobID, "features"),
		c.key("job", jobID, "llm_url"),
		c.key("job", jobID, "source_classification"),
		c.key("job", jobID, "micro_inferences"),
		c.key("job", jobID, "chunks"),
		c.key("job", jobID, "metadata:document"),
		c.key("job", jobID, "metadata:text"),
	}
	err := c.client.Del(ctx, keys...).Err()
	if err != nil {
		return fmt.Errorf("failed to delete job: %w", err)
	}
	return nil
}
```

---

## Task 2: Go Config, Broker, & Orchestrator Handlers

**Files:**
- Modify: `internal/config/config.go:25-28`
- Modify: `internal/broker/rabbitmq.go:136-152`
- Modify: `internal/models/job.go:86-90`
- Modify: `cmd/orchestrator/main.go:329-420`

### Step 2.1: Add InferencesQueue to config

In `internal/config/config.go`, add after line 28:

```go
	InferencesQueue    string        `env:"INFERENCES_QUEUE" default:"inferences"`
```

### Step 2.2: Add features + llm_url to CreateJobRequest

In `internal/models/job.go` at line 86, update:

```go
type CreateJobRequest struct {
	DocumentBase64 string   `json:"document_base64" binding:"required_without=DocumentURL"`
	DocumentURL    string   `json:"document_url" binding:"required_without=DocumentBase64"`
	Filename       string   `json:"filename,omitempty"`
	Features       []string `json:"features,omitempty"` // e.g. ["inferences"]
	LLMUrl         string   `json:"llm_url,omitempty"`  // e.g. "http://vllm:8000"
}
```

### Step 2.3: Update broker to declare inferences queue

In `internal/broker/rabbitmq.go`, update `declareQueues` method (line 136):

```go
func (b *RabbitMQBroker) declareQueues() error {
	queues := []string{
		b.config.ExtractQueue,
		b.config.EmbeddingsQueue,
		b.config.EntitiesQueue,
		b.config.MetadataQueue,
		b.config.InferencesQueue,
	}

	for _, queue := range queues {
		if err := b.declareQueue(queue); err != nil {
			return fmt.Errorf("failed to declare queue %s: %w", queue, err)
		}
		b.logger.Info().Msgf("Queue declared: %s", queue)
	}

	return nil
}
```

Also update `UpdateQueueMetrics` method (line 383):

```go
func (b *RabbitMQBroker) UpdateQueueMetrics() error {
	queues := []string{
		b.config.ExtractQueue,
		b.config.EmbeddingsQueue,
		b.config.EntitiesQueue,
		b.config.MetadataQueue,
		b.config.InferencesQueue,
	}

	for _, queue := range queues {
		info, err := b.GetQueueInfo(queue)
		if err != nil {
			b.logger.Warn().Err(err).Str("queue", queue).Msg("Failed to get queue info")
			continue
		}

		// Update queue depth metric
		metrics.QueueDepth.WithLabelValues(queue).Set(float64(info.Messages))
	}

	return nil
}
```

### Step 2.4: Update createJobHandler to store features & llm_url

In `cmd/orchestrator/main.go` at `createJobHandler` (line 329), after line 373 (after `SetJobStatus`), add:

```go
	// Store features and LLM URL in Redis
	if len(req.Features) > 0 {
		if err := redis.SetJobFeatures(ctx, jobID, req.Features); err != nil {
			logger.Warn().Err(err).Msgf("Failed to store job features: %v", err)
		}
	}
	
	if req.LLMUrl != "" {
		if err := redis.SetJobLLMURL(ctx, jobID, req.LLMUrl); err != nil {
			logger.Warn().Err(err).Msgf("Failed to store job LLM URL: %v", err)
		}
	}
```

Also update `uploadHandler` similarly. Find the equivalent `SetJobStatus` call in `uploadHandler` (around line 835) and add the same code.

---

## Task 3: Python Extraction-Worker Source Classifier

**Files:**
- Modify: `cmd/extraction-worker/worker.py:495-512`
- Create: `cmd/extraction-worker/tests/test_source_classifier.py`

### Step 3.1: Add SourceClassifier class to extraction-worker

In `cmd/extraction-worker/worker.py`, add at the top (after imports, before `ExtractionWorker` class):

```python
class SourceClassifier:
    """Classify document source/type using regex patterns."""
    
    # Regex patterns for different document types
    PATTERNS = {
        "notariado": [
            r"notario|notaría|protocolo|escritura|fedatario",
            r"fe pública|acta notarial",
        ],
        "catastro": [
            r"catastro|catastral|referencia catastral",
            r"plano catastral|datos catastrales",
        ],
        "bancario": [
            r"banco|bancaria|entidad financiera",
            r"estado de cuenta|extracto bancario|movimiento",
        ],
        "fiscal": [
            r"impuesto|declaración fiscal|renta",
            r"hacienda|tributario|aeat",
        ],
        "legal": [
            r"contrato|acuerdo|términos y condiciones",
            r"cláusula|párrafo|legal|juzgado",
        ],
    }
    
    @staticmethod
    def classify(text: str) -> Optional[Dict[str, Any]]:
        """
        Classify document source using regex patterns.
        Returns {"document_type": str, "confidence": float, "classifier_version": str}
        """
        if not text:
            return None
        
        text_lower = text.lower()
        scores = {}
        
        for doc_type, patterns in SourceClassifier.PATTERNS.items():
            matches = 0
            for pattern in patterns:
                import re
                if re.search(pattern, text_lower, re.IGNORECASE):
                    matches += 1
            
            if matches > 0:
                # Confidence based on number of matching patterns
                confidence = min(1.0, matches / len(patterns))
                scores[doc_type] = confidence
        
        if not scores:
            return None
        
        # Return highest confidence match
        best_type = max(scores.items(), key=lambda x: x[1])
        return {
            "document_type": best_type[0],
            "confidence": float(best_type[1]),
            "classifier_version": "1.0",
        }
```

### Step 3.2: Call classifier in extraction-worker process method

In `cmd/extraction-worker/worker.py`, find the `process` method (around line 480), and after line 510 (after metadata is set in Redis), add:

```python
            # Classify document source
            try:
                classification = SourceClassifier.classify(text)
                if classification:
                    self.redis_client.set(
                        f"orchestrator:job:{job_id}:source_classification",
                        json.dumps(classification),
                    )
                    logger.info(
                        f"Document classified as: {classification['document_type']} "
                        f"(confidence={classification['confidence']:.2f})"
                    )
            except Exception as e:
                logger.warning(f"Source classification failed: {e}")
                # Continue anyway - classification is optional
```

### Step 3.3: Write tests for SourceClassifier

Create `cmd/extraction-worker/tests/test_source_classifier.py`:

```python
import pytest
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))

from extraction_worker.worker import SourceClassifier


class TestSourceClassifier:
    def test_notariado_classification(self):
        text = "ESCRITURA NOTARIAL de compraventa. El notario fedatario certifica..."
        result = SourceClassifier.classify(text)
        assert result is not None
        assert result["document_type"] == "notariado"
        assert result["confidence"] > 0.5

    def test_catastro_classification(self):
        text = "Datos catastrales. Referencia catastral: 12345678. Plano catastral adjunto."
        result = SourceClassifier.classify(text)
        assert result is not None
        assert result["document_type"] == "catastro"

    def test_bancario_classification(self):
        text = "ESTADO DE CUENTA. Banco XYZ. Extracto bancario del período. Movimientos registrados."
        result = SourceClassifier.classify(text)
        assert result is not None
        assert result["document_type"] == "bancario"

    def test_unknown_document(self):
        text = "This is just a random text with no identifiable document markers."
        result = SourceClassifier.classify(text)
        # Should return None or lowest confidence match
        assert result is None or result["confidence"] < 0.5

    def test_empty_text(self):
        result = SourceClassifier.classify("")
        assert result is None
```

Run: `pytest cmd/extraction-worker/tests/test_source_classifier.py -v`

Expected: 5/5 tests passing

---

## Task 4: Python Entities-Worker Inference Trigger

**Files:**
- Modify: `cmd/entities-worker/worker.py:688-700`

### Step 4.1: Add inference queue trigger after entity extraction

In `cmd/entities-worker/worker.py`, find where steps are marked completed (around line 688), and after the `hset` for "entities" step, add:

```python
            # Check if micro-inferences are requested
            try:
                features_json = self.redis_client.get(f"orchestrator:job:{job_id}:features")
                if features_json:
                    import json
                    features = json.loads(features_json)
                    if "inferences" in features:
                        # Publish to inferences queue
                        import pika
                        from pkg.worker_common.rabbitmq import parse_rabbitmq_url, connect_rabbitmq
                        
                        RABBITMQ_URL = os.getenv("RABBITMQ_URL", "amqp://rabbitmq:5672/")
                        params = parse_rabbitmq_url(RABBITMQ_URL)
                        connection = pika.BlockingConnection(params)
                        channel = connection.channel()
                        
                        inference_msg = {"job_id": job_id}
                        channel.basic_publish(
                            exchange="",
                            routing_key="inferences",
                            body=json.dumps(inference_msg),
                            properties=pika.BasicProperties(delivery_mode=2),
                        )
                        logger.info(f"Published inference task for job {job_id}")
                        connection.close()
            except Exception as e:
                logger.warning(f"Failed to trigger inference: {e}")
                # Continue anyway - inference is optional
```

---

## Task 5: New Inference-Worker Python Service

**Files:**
- Create: `cmd/inference-worker/worker.py`
- Create: `cmd/inference-worker/requirements.txt`
- Create: `cmd/inference-worker/Dockerfile`
- Create: `cmd/inference-worker/tests/test_inference_worker.py`

### Step 5.1: Create requirements.txt

```
pika>=1.3.0
redis>=5.0.0
requests>=2.31.0
prometheus-client>=0.19.0
python-dotenv>=1.0.0
```

### Step 5.2: Create Dockerfile

```dockerfile
FROM python:3.11.12-slim-bookworm

LABEL maintainer="ia-text-orchestrator"
LABEL description="Inference Worker for Micro-Inferences"

WORKDIR /app

ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

COPY cmd/inference-worker/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

RUN adduser --disabled-password --gecos "" app && \
    chown -R app:app /app

COPY pkg/ ./pkg/
COPY cmd/inference-worker/ ./cmd/inference-worker/

USER app

CMD ["python", "cmd/inference-worker/worker.py"]
```

### Step 5.3: Create inference-worker/worker.py

```python
#!/usr/bin/env python3
"""
Inference Worker for IA Text Orchestrator
Consumes messages from RabbitMQ and extracts micro-inferences using an LLM
"""

import os
import sys
import json
import logging
import time
import redis
import pika
import requests
from typing import Dict, List, Any, Optional
from prometheus_client import Counter, Histogram, start_http_server

sys.path.insert(0, "/app")
from pkg.worker_common.rabbitmq import parse_rabbitmq_url, connect_rabbitmq, declare_queue
from pkg.events_python import EventBus

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379")
RABBITMQ_URL = os.getenv("RABBITMQ_URL", "amqp://rabbitmq:5672/")
QUEUE_NAME = os.getenv("QUEUE_NAME", "inferences")
METRICS_PORT = int(os.getenv("METRICS_PORT", "8006"))

# Prometheus metrics
jobs_total = Counter("inference_worker_jobs_total", "Total jobs processed", ["status"])
job_duration = Histogram("inference_worker_job_duration_seconds", "Job duration")


class InferenceWorker:
    def __init__(self):
        self.redis_client = redis.from_url(REDIS_URL, decode_responses=True)
        self.event_bus = EventBus(self.redis_client)

    def extract_inferences(
        self, text: str, llm_url: str, max_inferences: int = 5
    ) -> List[Dict[str, Any]]:
        """
        Extract micro-inferences from text using an LLM.
        
        Args:
            text: Document text to extract inferences from
            llm_url: URL of the LLM service (e.g., vLLM)
            max_inferences: Maximum number of inferences to extract
            
        Returns:
            List of {"fact": str, "confidence": float, "source": "llm"}
        """
        if not llm_url:
            logger.warning("No LLM URL configured, skipping inferences")
            return []

        try:
            # Truncate text to first 2000 chars for LLM context
            truncated_text = text[:2000]
            
            prompt = f"""Extract up to {max_inferences} key facts from the following document text.
Return ONLY a JSON array of objects with "fact" and "confidence" (0.0-1.0) keys.
Example: [{{"fact": "The property value is 500,000 EUR", "confidence": 0.95}}]

Document text:
{truncated_text}

Facts:"""

            # Call LLM
            payload = {
                "prompt": prompt,
                "max_tokens": 500,
                "temperature": 0.1,
            }
            
            response = requests.post(
                f"{llm_url}/v1/completions",
                json=payload,
                timeout=30,
            )
            response.raise_for_status()
            
            result = response.json()
            completion_text = result.get("choices", [{}])[0].get("text", "")
            
            # Parse JSON from LLM response
            import re
            json_match = re.search(r"\[.*\]", completion_text, re.DOTALL)
            if not json_match:
                logger.warning("No JSON found in LLM response")
                return []
            
            inferences = json.loads(json_match.group())
            
            # Validate and annotate
            validated = []
            for inf in inferences:
                if isinstance(inf, dict) and "fact" in inf:
                    validated.append({
                        "fact": inf.get("fact", ""),
                        "confidence": float(inf.get("confidence", 0.5)),
                        "source": "llm",
                    })
            
            logger.info(f"Extracted {len(validated)} inferences from LLM")
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

    def process(self, ch, method, properties, body):
        start_time = time.time()
        job_id = None

        try:
            message = json.loads(body)
            job_id = message.get("job_id")

            logger.info(f"Processing inferences for job: {job_id}")

            # Get text from Redis
            text = self.redis_client.get(f"orchestrator:job:{job_id}:text")
            if not text:
                logger.warning(f"No text found in Redis for job: {job_id}")
                jobs_total.labels(status="no_text").inc()
                ch.basic_nack(delivery_tag=method.delivery_tag, requeue=False)
                return

            # Get LLM URL from Redis
            llm_url = self.redis_client.get(f"orchestrator:job:{job_id}:llm_url")
            if not llm_url:
                logger.warning(f"No LLM URL configured for job: {job_id}")
                jobs_total.labels(status="no_llm_url").inc()
                ch.basic_nack(delivery_tag=method.delivery_tag, requeue=False)
                return

            # Extract inferences
            inferences = self.extract_inferences(text, llm_url)

            # Store in Redis
            inferences_key = f"orchestrator:job:{job_id}:micro_inferences"
            self.redis_client.set(inferences_key, json.dumps(inferences))

            # Mark step as completed
            self.redis_client.hset(
                f"orchestrator:job:{job_id}:steps", "inferences", "completed"
            )

            self.event_bus.publish_job_progress(job_id, 80, "inferences")

            duration = time.time() - start_time
            job_duration.observe(duration)
            jobs_total.labels(status="success").inc()

            logger.info(
                f"Inferences completed for job: {job_id} in {duration:.2f}s, "
                f"extracted {len(inferences)} inferences"
            )

            ch.basic_ack(delivery_tag=method.delivery_tag)

        except Exception as e:
            logger.error(f"Error processing inferences: {e}")
            jobs_total.labels(status="error").inc()
            ch.basic_nack(delivery_tag=method.delivery_tag, requeue=True)


def signal_handler(signum, frame):
    logger.info("Received shutdown signal, stopping worker...")
    sys.exit(0)


def main():
    import signal
    
    logger.info("Starting Inference Worker")
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    start_http_server(METRICS_PORT)
    logger.info(f"Metrics server started on port {METRICS_PORT}")

    worker = InferenceWorker()

    while True:
        try:
            with connect_rabbitmq(RABBITMQ_URL) as (connection, channel):
                logger.info(f"Consuming from queue: {QUEUE_NAME}")

                declare_queue(channel, QUEUE_NAME)
                channel.basic_consume(
                    queue=QUEUE_NAME, on_message_callback=worker.process, auto_ack=False
                )

                channel.start_consuming()

        except Exception as e:
            logger.error(f"RabbitMQ connection error: {e}")
            time.sleep(5)


if __name__ == "__main__":
    main()
```

### Step 5.4: Write tests

Create `cmd/inference-worker/tests/test_inference_worker.py`:

```python
import pytest
import json
import sys
import os
from unittest.mock import Mock, patch, MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))

from inference_worker.worker import InferenceWorker


class TestInferenceWorker:
    @pytest.fixture
    def worker(self):
        with patch("redis.from_url"):
            return InferenceWorker()

    def test_extract_inferences_success(self, worker):
        """Test successful inference extraction"""
        text = "The property has a value of 500,000 EUR and was built in 2010."
        llm_url = "http://localhost:8000"

        with patch("requests.post") as mock_post:
            mock_response = Mock()
            mock_response.json.return_value = {
                "choices": [{"text": '[{"fact": "Property value is 500,000 EUR", "confidence": 0.95}]'}]
            }
            mock_post.return_value = mock_response

            inferences = worker.extract_inferences(text, llm_url)

            assert len(inferences) == 1
            assert inferences[0]["fact"] == "Property value is 500,000 EUR"
            assert inferences[0]["confidence"] == 0.95
            assert inferences[0]["source"] == "llm"

    def test_extract_inferences_no_llm_url(self, worker):
        """Test with no LLM URL configured"""
        inferences = worker.extract_inferences("Some text", "")
        assert inferences == []

    def test_extract_inferences_llm_failure(self, worker):
        """Test LLM call failure"""
        with patch("requests.post") as mock_post:
            mock_post.side_effect = Exception("Connection failed")
            inferences = worker.extract_inferences("Some text", "http://localhost:8000")
            assert inferences == []
```

Run: `pytest cmd/inference-worker/tests/test_inference_worker.py -v`

Expected: 3/3 tests passing

---

## Task 6: Completion-Worker Update

**Files:**
- Modify: `cmd/completion-worker/worker.py:47-54` (required_steps)
- Modify: `cmd/completion-worker/worker.py:187-254` (finalize_job)

### Step 6.1: Update required_steps logic

In `cmd/completion-worker/worker.py`, update `__init__` method (line 47):

```python
    def __init__(self):
        self.redis_client = redis.from_url(REDIS_URL, decode_responses=True)
        self.event_bus = EventBus(self.redis_client)
        # Default required steps for full pipeline
        self.default_required_steps = {
            "extraction",
            "embeddings",
            "entities",
            "metadata",
        }
        # Spreadsheet pipeline (no embeddings, no metadata)
        self.spreadsheet_required_steps = {"extraction", "entities"}
```

In `check_job_completion` method (around line 142), update the logic to handle features:

```python
    def check_job_completion(self, job_id: str):
        try:
            steps = self.redis_client.hgetall(f"orchestrator:job:{job_id}:steps")

            completed_steps = set()
            for step, status in steps.items():
                if status == "completed":
                    completed_steps.add(step)

            logger.info(f"Job {job_id} completed steps: {completed_steps}")

            # Determine required steps based on document type and features
            document_metadata_json = self.redis_client.get(
                f"orchestrator:job:{job_id}:metadata:document"
            )
            document_metadata = (
                json.loads(document_metadata_json) if document_metadata_json else {}
            )
            mime_type = document_metadata.get("mime_type", "")

            # Check if it's a spreadsheet
            is_spreadsheet = "spreadsheet" in mime_type.lower() or mime_type in [
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                "application/vnd.ms-excel",
                "text/csv",
                "application/zip",  # Excel files may show as ZIP
            ]

            required_steps = (
                self.spreadsheet_required_steps
                if is_spreadsheet
                else self.default_required_steps.copy()
            )
            
            # Add inferences if features were requested
            features_json = self.redis_client.get(f"orchestrator:job:{job_id}:features")
            if features_json:
                try:
                    features = json.loads(features_json)
                    if "inferences" in features:
                        required_steps.add("inferences")
                except Exception as e:
                    logger.warning(f"Failed to parse features: {e}")

            logger.info(
                f"Job {job_id} document type: {'spreadsheet' if is_spreadsheet else 'full'}, "
                f"required steps: {required_steps}"
            )

            if required_steps.issubset(completed_steps):
                self.finalize_job(job_id)

        except Exception as e:
            logger.error(f"Error checking job completion: {e}")
```

### Step 6.2: Update finalize_job to include new results

In `finalize_job` method (line 187), update the Redis pipeline to fetch new keys:

```python
    def finalize_job(self, job_id: str):
        finalization_start_time = time.time()
        try:
            logger.info(f"Finalizing job: {job_id}")

            # Use Redis pipeline to fetch all required data in a single round-trip
            pipe = self.redis_client.pipeline()
            pipe.hgetall(f"orchestrator:job:{job_id}:meta")
            pipe.hgetall(f"orchestrator:job:{job_id}:status")
            pipe.get(f"orchestrator:job:{job_id}:text")
            pipe.get(f"orchestrator:job:{job_id}:metadata:document")
            pipe.get(f"orchestrator:job:{job_id}:metadata:text")
            pipe.get(f"orchestrator:job:{job_id}:chunks")
            pipe.get(f"orchestrator:job:{job_id}:embeddings")
            pipe.get(f"orchestrator:job:{job_id}:entities_raw")
            pipe.get(f"orchestrator:job:{job_id}:source_classification")
            pipe.get(f"orchestrator:job:{job_id}:micro_inferences")
            (
                meta,
                status_data,
                text,
                document_metadata_json,
                text_metadata_json,
                chunks_json,
                embeddings_json,
                entities_raw_json,
                source_classification_json,
                micro_inferences_json,
            ) = pipe.execute()

            created_at_timestamp = int(meta.get("created_at", time.time()))
            created_at = datetime.fromtimestamp(created_at_timestamp).isoformat()
            completed_at = datetime.fromtimestamp(int(time.time())).isoformat()

            if status_data and status_data.get("status") == "completed":
                logger.info(f"Job {job_id} already finalized, skipping")
                return

            text = text or ""

            document_metadata = (
                json.loads(document_metadata_json) if document_metadata_json else {}
            )

            text_metadata = json.loads(text_metadata_json) if text_metadata_json else {}

            chunks = json.loads(chunks_json) if chunks_json else []

            embeddings_raw = json.loads(embeddings_json) if embeddings_json else {}
            embeddings = {"model": "BAAI/bge-m3", "dimension": 1024, **embeddings_raw}

            # Read RAW entities from entities-worker (before dedup)
            entities_raw = json.loads(entities_raw_json) if entities_raw_json else []

            # Apply deduplication at the end (now that we have all entities from all chunks)
            entities = self.deduplicate_entities(entities_raw) if entities_raw else []

            logger.info(
                f"Entities: {len(entities_raw)} raw → {len(entities)} after dedup"
            )

            # Parse source classification
            source_classification = None
            if source_classification_json:
                source_classification = json.loads(source_classification_json)

            # Parse micro inferences
            micro_inferences = json.loads(micro_inferences_json) if micro_inferences_json else []

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
            
            # Add optional fields only if present
            if source_classification:
                results["source_classification"] = source_classification
            if micro_inferences:
                results["micro_inferences"] = micro_inferences

            self.redis_client.set(
                f"orchestrator:job:{job_id}:results",
                json.dumps(results, ensure_ascii=False),
            )

            self.redis_client.hset(
                f"orchestrator:job:{job_id}:meta", "completed_at", str(int(time.time()))
            )

            self.redis_client.hset(
                f"orchestrator:job:{job_id}:status", "status", "completed"
            )

            self.save_results_to_file(job_id, results)
            self.send_webhook(job_id, "completed", None)

            self.event_bus.publish_job_completed(job_id)

            logger.info(
                f"Job {job_id} finalized: chunks={len(chunks)}, entities={len(entities)}, "
                f"inferences={len(micro_inferences)}"
            )

            # Record metrics
            job_finalization_duration.observe(time.time() - finalization_start_time)
            jobs_finalized_total.labels(status="success").inc()

        except Exception as e:
            logger.error(f"Error finalizing job: {e}", exc_info=True)
            self.redis_client.hset(
                f"orchestrator:job:{job_id}:status", "status", "failed"
            )
            self.redis_client.set(
                f"orchestrator:job:{job_id}:error", f"Finalization error: {str(e)}"
            )
            self.send_webhook(job_id, "failed", str(e))
            self.event_bus.publish_job_failed(job_id, str(e))

            # Record failure metrics
            job_finalization_duration.observe(time.time() - finalization_start_time)
            jobs_finalized_total.labels(status="error").inc()
```

---

## Task 7: Docker Compose - Add Inference-Worker

**Files:**
- Modify: `deploy/docker/docker-compose.yml:296-373`

### Step 7.1: Add inference-worker service

In `deploy/docker/docker-compose.yml`, add after `completion-worker` service (around line 316):

```yaml
  inference-worker:
    build:
      context: ../../
      dockerfile: cmd/inference-worker/Dockerfile
    container_name: ia-text-inference-worker
    environment:
    - REDIS_URL=redis://redis:6379
    - RABBITMQ_URL=amqp://${RABBITMQ_USER:-guest}:${RABBITMQ_PASS:-guest}@rabbitmq:5672
    - LOG_LEVEL=info
    - QUEUE_NAME=inferences
    - METRICS_PORT=8006
    depends_on:
      rabbitmq:
        condition: service_healthy
      redis:
        condition: service_healthy
    networks:
    - backend
    - datastore
    restart: unless-stopped
    deploy:
      resources:
        limits:
          cpus: '1'
          memory: 2G
        reservations:
          memory: 512M
```

### Step 7.2: Update orchestrator environment to include INFERENCES_QUEUE

In `deploy/docker/docker-compose.yml`, find the orchestrator service (around line 48) and add:

```yaml
    - INFERENCES_QUEUE=inferences
```

---

## Testing & Verification

After all tasks complete:

1. **Build the stack:** `make docker-up`
2. **Test extraction + source classification:**
   ```bash
   curl -X POST http://localhost:8080/v1/documents/process \
     -H "Content-Type: application/json" \
     -d '{
       "document_base64": "...",
       "features": ["inferences"],
       "llm_url": "http://vllm:8000"
     }'
   ```
3. **Check results:** `curl http://localhost:8080/v1/documents/{job_id}`
4. **Verify output includes:**
   - `source_classification` (always, if regex matched)
   - `micro_inferences` (if features=["inferences"] AND LLM available)

---

## Rollback Plan

If deployment issues occur:
1. Remove `inference-worker` service from docker-compose
2. Set `INFERENCES_QUEUE` to empty/unused queue name in orchestrator
3. Remove inference trigger from entities-worker
4. Existing pipeline will continue with embeddings+entities+metadata only
5. No data loss — all changes are additive

