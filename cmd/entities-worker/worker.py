#!/usr/bin/env python3
"""
Entities Worker for IA Text Orchestrator
Consumes messages from RabbitMQ and extracts entities using GLiNER
"""

# Configure cache paths BEFORE importing transformers/gliner
# Do NOT set HF_HUB_OFFLINE=1 or TRANSFORMERS_OFFLINE=1 - these break GLiNER
import os
os.environ["HF_HOME"] = "/home/app/.cache/huggingface"
os.environ["TRANSFORMERS_CACHE"] = "/home/app/.cache/huggingface"
os.environ["HF_DATASETS_CACHE"] = "/home/app/.cache/huggingface"
os.environ["TOKENIZERS_PARALLELISM"] = "false"  # Prevent hanging in tokenizer initialization

import json
import logging
import signal
import sys
import time
from contextlib import contextmanager
from typing import Dict, List, Optional, Any
from pathlib import Path

import pika
import redis
import requests
from prometheus_client import Counter, Histogram, Gauge, start_http_server
from rapidfuzz import fuzz
from unidecode import unidecode

sys.path.insert(0, "/app")
from pkg.events_python import EventBus
from sliding_window import (
    process_with_sliding_window,
    estimate_tokens,
    requires_sliding_window,
)

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

jobs_total = Counter("entities_worker_jobs_total", "Total jobs processed", ["status"])
job_duration = Histogram("entities_worker_job_duration_seconds", "Job duration")
gpu_available = Gauge("entities_worker_gpu_available", "GPU availability", ["device"])
entities_deduplicated = Counter(
    "entities_worker_deduplicated_total", "Total entities deduplicated"
)

REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379")
RABBITMQ_URL = os.getenv("RABBITMQ_URL", "amqp://rabbitmq:5672/")
QUEUE_NAME = os.getenv("QUEUE_NAME", "entities")
METRICS_PORT = int(os.getenv("METRICS_PORT", "8002"))
GLINER_MODEL_PATH = os.getenv("GLINER_MODEL_PATH", "/models/gliner_multitask-v0.5")
GLINER_MODEL_NAME = os.getenv("GLINER_MODEL_NAME", "urchade/gliner_large-v2.1")
HF_CACHE_DIR = os.getenv("HF_CACHE_DIR", "/root/.cache/huggingface")
ENTITY_TYPES = os.getenv("ENTITY_TYPES", "PER,ORG,LOC,DATE,MONEY")
ALLOW_REMOTE_DOWNLOAD = os.getenv("ALLOW_REMOTE_DOWNLOAD", "true").lower() == "true"
DEDUPLICATION_ENABLED = os.getenv("DEDUPLICATION_ENABLED", "false").lower() == "true"
FUZZY_MATCH_THRESHOLD = float(os.getenv("FUZZY_MATCH_THRESHOLD", "0.85"))

# Thresholds per entity type
ENTITY_THRESHOLDS = {
    "PER": float(os.getenv("ENTITY_THRESHOLD_PER", "0.35")),
    "ORG": float(os.getenv("ENTITY_THRESHOLD_ORG", "0.50")),
    "LOC": float(os.getenv("ENTITY_THRESHOLD_LOC", "0.50")),
    "DATE": float(os.getenv("ENTITY_THRESHOLD_DATE", "0.45")),
    "MONEY": float(os.getenv("ENTITY_THRESHOLD_MONEY", "0.55")),
}


class EntitiesWorker:
    def __init__(self):
        self.redis_client = redis.from_url(REDIS_URL, decode_responses=True)
        self.event_bus = EventBus(self.redis_client)
        self.model = None
        self.device = "cpu"
        self.default_entities = [e.strip() for e in ENTITY_TYPES.split(",")]

    def load_model(self):
        from gliner import GLiNER
        
        # Model path
        model_path = os.getenv("GLINER_MODEL_PATH", "/models/gliner_model")
        deberta_path = "/models/deberta-v3-large"
        
        logger.info("=" * 70)
        logger.info("🔍 Loading GLiNER Model (Offline Mode)")
        logger.info("=" * 70)
        logger.info(f"   GLiNER model path: {model_path}")
        logger.info(f"   DeBERTa backbone path: {deberta_path}")
        logger.info(f"   HF_HOME: {os.environ.get('HF_HOME')}")
        
        try:
            # Verify GLiNER model directory exists
            model_path_obj = Path(model_path)
            if not model_path_obj.exists():
                raise FileNotFoundError(f"GLiNER model directory not found: {model_path}")
            
            # Check for gliner_config.json (required)
            config_file = model_path_obj / "gliner_config.json"
            if not config_file.exists():
                raise FileNotFoundError(f"gliner_config.json not found in {model_path}")
            
            # Check for model weights
            model_files = list(model_path_obj.glob("*.bin")) + list(model_path_obj.glob("*.safetensors"))
            if not model_files:
                raise FileNotFoundError(f"No model weight files (.bin or .safetensors) found in {model_path}")
            
            logger.info(f"   ✓ GLiNER config found: gliner_config.json")
            logger.info(f"   ✓ GLiNER model weights found: {len(model_files)} file(s)")
            
            # Verify DeBERTa backbone exists
            deberta_path_obj = Path(deberta_path)
            if not deberta_path_obj.exists():
                raise FileNotFoundError(f"DeBERTa backbone directory not found at {deberta_path}")
            
            # Check for critical tokenizer files
            # DeBERTa-v3-large uses SentencePiece (spm.model), not fast tokenizer (tokenizer.json)
            critical_files = ["spm.model", "tokenizer_config.json", "config.json"]
            missing_files = [f for f in critical_files if not (deberta_path_obj / f).exists()]
            if missing_files:
                raise FileNotFoundError(f"Missing DeBERTa tokenizer files in {deberta_path}: {missing_files}")
            
            deberta_files = len(list(deberta_path_obj.glob("*")))
            logger.info(f"   ✓ DeBERTa backbone found: {deberta_files} file(s)")
            logger.info(f"   ✓ Tokenizer files verified")
            
            # Load GLiNER with offline mode
            logger.info("\n🚀 Loading GLiNER...")
            try:
                self.model = GLiNER.from_pretrained(
                    model_path,
                    local_files_only=True  # Force offline mode
                )
                logger.info("   ✓ GLiNER model loaded successfully")
            except KeyboardInterrupt:
                logger.error("   ❌ GLiNER loading was interrupted")
                raise
            except Exception as e:
                logger.error(f"   ❌ GLiNER loading failed: {e}")
                raise
            
            logger.info("=" * 70)
            logger.info("✅ GLiNER Model Loaded Successfully")
            logger.info("=" * 70)
            logger.info(f"   Model type: {type(self.model).__name__}")
            logger.info(f"   Device: {self.device}")
            logger.info(f"   Ready for entity extraction")
            logger.info("=" * 70)

        except Exception as e:
            logger.error("=" * 70)
            logger.error("❌ FAILED TO LOAD GLiNER MODEL")
            logger.error("=" * 70)
            logger.error(f"   Error: {e}")
            logger.error(f"   GLiNER path: {model_path}")
            logger.error(f"   DeBERTa path: {deberta_path}")
            
            import traceback
            logger.error("\nFull traceback:")
            traceback.print_exc()
            
            logger.error("\n" + "=" * 70)
            logger.error("Troubleshooting:")
            logger.error(f"  1. Check GLiNER files: ls -la {model_path}/")
            logger.error(f"  2. Check DeBERTa files: ls -la {deberta_path}/")
            logger.error(f"  3. Verify gliner_config.json has: \"model_name\": \"/models/deberta-v3-large\"")
            logger.error("=" * 70)
            
            raise Exception(f"Failed to load GLiNER model: {e}")

    def _extract_dates(self, text: str) -> List[Dict]:
        import re

        dates = []
        patterns = [
            r"\\d{1,2}/\\d{1,2}/\\d{2,4}",
            r"\\d{1,2}-\\d{1,2}-\\d{2,4}",
            r"\\d{4}-\\d{2}-\\d{2}",
            r"(?:January|February|March|April|May|June|July|August|September|October|November|December)\\s+\\d{1,2},?\\s+\\d{4}",
            r"\\d{1,2}\\s+(?:January|February|March|April|May|June|July|August|September|October|November|December)\\s+\\d{4}",
            r"(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),?\\s+\\d{1,2}\\s+(?:January|February|March|April|May|June|July|August|September|October|November|December)",
        ]
        for pattern in patterns:
            for match in re.finditer(pattern, text, re.IGNORECASE):
                dates.append(
                    {
                        "text": match.group(),
                        "label": "DATE",
                        "confidence": 0.75,
                        "start": match.start(),
                        "end": match.end(),
                    }
                )
        return dates

    def _extract_money(self, text: str) -> List[Dict]:
        import re

        money = []
        patterns = [
            r"\\$\\d+(?:,\\d{3})*(?:\\.\\d{2})?",
            r"\\d+(?:,\\d{3})*(?:\\.\\d{2})?\\s*(?:USD|EUR|GBP)",
            r"€\\d+(?:,\\d{3})*(?:\\.\\d{2})?",
            r"£\\d+(?:,\\d{3})*(?:\\.\\d{2})?",
        ]
        for pattern in patterns:
            for match in re.finditer(pattern, text, re.IGNORECASE):
                money.append(
                    {
                        "text": match.group(),
                        "label": "MONEY",
                        "confidence": 0.8,
                        "start": match.start(),
                        "end": match.end(),
                    }
                )
        return money

    def _extract_orgs(self, text: str) -> List[Dict]:
        import re

        orgs = []
        patterns = [
            r"(?:Inc\\.|LLC|Corp\\.|Ltd\\.|S\\.A\\.|S\\.L\\.|B\\.V\\.|GmbH)",
            r"(?:University|Institute|Foundation|Association|Corporation)",
            r"(?:Bank|Insurance|Financial|Media|Tech|Software|Hardware)",
        ]
        for pattern in patterns:
            for match in re.finditer(pattern, text, re.IGNORECASE):
                orgs.append(
                    {
                        "text": match.group(),
                        "label": "ORG",
                        "confidence": 0.6,
                        "start": match.start(),
                        "end": match.end(),
                    }
                )
        return orgs

    def _extract_locs(self, text: str) -> List[Dict]:
        import re

        locs = []
        patterns = [
            r"(?:New York|Los Angeles|Chicago|Houston|Phoenix|Philadelphia|San Antonio|San Diego)",
            r"(?:Madrid|Barcelona|Valencia|Sevilla|Málaga|Bilbao)",
            r"(?:Spain|France|Germany|Italy|United Kingdom|United States)",
        ]
        for pattern in patterns:
            for match in re.finditer(pattern, text, re.IGNORECASE):
                locs.append(
                    {
                        "text": match.group(),
                        "label": "LOC",
                        "confidence": 0.65,
                        "start": match.start(),
                        "end": match.end(),
                    }
                )
        return locs

    def _extract_persons(self, text: str) -> List[Dict]:
        import re

        persons = []
        pattern = r"\\b[A-Z][a-z]+\\s+[A-Z][a-z]+\\b"
        exclude = {
            "The",
            "This",
            "That",
            "What",
            "When",
            "Where",
            "Which",
            "Who",
            "How",
            "There",
        }
        for match in re.finditer(pattern, text):
            name = match.group()
            if name not in exclude:
                persons.append(
                    {
                        "text": name,
                        "label": "PER",
                        "confidence": 0.5,
                        "start": match.start(),
                        "end": match.end(),
                    }
                )
        return persons

    def predict_entities(
        self, text: str, entity_types: List[str], threshold: float = 0.5
    ) -> List[Dict]:
        """
        Predict entities using rule-based patterns and heuristics.

        Args:
            text: Input text to extract entities from
            entity_types: List of entity types to extract
            threshold: Minimum confidence score

        Returns:
            List of entity predictions with text, label, score, start, end
        """
        try:
            entities = self.model.predict_entities(
                text,
                entity_types,
                threshold=threshold,
            )
            return entities if entities else []
        except Exception as e:
            logger.error(f"Error predicting entities: {e}")
            return []

    def normalize_entity_text(self, text: str) -> str:
        """Normalize entity text for deduplication."""
        return unidecode(text).lower().strip()

    def deduplicate_entities(
        self, entities: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """
        Deduplicate entities using fuzzy matching and normalization.

        Args:
            entities: List of extracted entities with positions

        Returns:
            List of deduplicated entities
        """
        if not DEDUPLICATION_ENABLED or not entities:
            return entities

        deduplicated = {}
        original_count = len(entities)

        for entity in entities:
            text = entity["text"]
            label = entity["label"]
            normalized_key = f"{label}:{self.normalize_entity_text(text)}"

            # Check if we already have a similar entity
            found_similar = False
            for existing_key, existing_entity in deduplicated.items():
                if existing_key.startswith(f"{label}:"):
                    # Calculate similarity
                    similarity = (
                        fuzz.ratio(
                            self.normalize_entity_text(text),
                            self.normalize_entity_text(existing_entity["text"]),
                        )
                        / 100.0
                    )

                    if similarity >= FUZZY_MATCH_THRESHOLD:
                        # Update with higher confidence if needed
                        if entity.get("confidence", 0) > existing_entity.get(
                            "confidence", 0
                        ):
                            existing_entity["confidence"] = entity["confidence"]
                            existing_entity["text"] = text  # Keep original text

                        # Track positions (optional, for multiple occurrences)
                        if "positions" not in existing_entity:
                            existing_entity["positions"] = []
                        existing_entity["positions"].append(
                            {
                                "chunk_id": entity.get("chunk_id"),
                                "start": entity.get("start"),
                                "end": entity.get("end"),
                            }
                        )

                        found_similar = True
                        break

            if not found_similar:
                # New unique entity
                deduplicated[normalized_key] = entity.copy()
                deduplicated[normalized_key]["positions"] = []

        # Convert back to list
        result = list(deduplicated.values())

        if len(result) < original_count:
            dedup_count = original_count - len(result)
            logger.info(
                f"Deduplicated {dedup_count} entities ({original_count} -> {len(result)})"
            )
            entities_deduplicated.inc(dedup_count)

        return result

    def calculate_global_position(
        self, chunk_offset: int, local_start: int, local_end: int
    ) -> tuple:
        """
        Calculate global position in document from chunk offset and local positions.

        Args:
            chunk_offset: Start position of chunk in document
            local_start: Start position within chunk
            local_end: End position within chunk

        Returns:
            Tuple of (global_start, global_end)
        """
        global_start = chunk_offset + local_start
        global_end = chunk_offset + local_end
        return (global_start, global_end)

    def process(self, ch, method, properties, body):
        start_time = time.time()
        job_id = None

        try:
            message = json.loads(body)
            job_id = message.get("job_id")
            chunks = message.get("chunks", [])

            entity_types = message.get("entity_types", self.default_entities)

            logger.info(
                f"Processing entities for job: {job_id} with {len(chunks)} chunks"
            )
            logger.info(f"Entity types: {entity_types}")

            if not chunks:
                chunks_json = self.redis_client.get(f"orchestrator:job:{job_id}:chunks")
                if chunks_json:
                    chunks = json.loads(chunks_json)
                else:
                    logger.warning(
                        f"No chunks found in message or Redis for job: {job_id}"
                    )
                    jobs_total.labels(status="no_chunks").inc()
                    ch.basic_nack(delivery_tag=method.delivery_tag, requeue=False)
                    return

            all_entities = []

            # Process each chunk
            for chunk in chunks:
                chunk_id = chunk.get("chunk_id")
                chunk_text = chunk.get("text", "")
                # Get chunk offset from either field name (support both formats)
                chunk_offset = chunk.get("start_offset") or chunk.get("offset") or 0

                if not chunk_text:
                    continue

                try:
                    # Use sliding window processor if chunk text is large
                    estimated_tokens = estimate_tokens(chunk_text)
                    
                    if requires_sliding_window(chunk_text):
                        logger.info(
                            f"Chunk {chunk_id}: {estimated_tokens} tokens detected, "
                            f"using sliding window approach"
                        )
                        # Define a wrapper function for predict_entities
                        def predict_with_thresholds(text, entity_types, threshold=0.1):
                            return self.model.predict_entities(
                                text, entity_types, threshold=threshold
                            )
                        
                        entities_items = process_with_sliding_window(
                            chunk_text,
                            predict_with_thresholds,
                            entity_types,
                            threshold=0.1,
                        )
                    else:
                        logger.debug(
                            f"Chunk {chunk_id}: {estimated_tokens} tokens, "
                            f"processing directly (no sliding window needed)"
                        )
                        # Direct processing for small chunks
                        entities = self.model.predict_entities(
                            chunk_text,
                            entity_types,
                            threshold=0.1,  # Use low threshold, filter per-type below
                        )

                        if entities and len(entities) > 0 and isinstance(entities[0], list):
                            entities_items = entities[0]
                        elif entities and isinstance(entities, list):
                            entities_items = entities
                        else:
                            entities_items = []

                    # Filter by per-type threshold
                    for e in entities_items:
                        label = e.get("label", "")
                        score = e.get("score", 0.0)

                        # Get threshold for this entity type
                        threshold = ENTITY_THRESHOLDS.get(label, 0.5)

                        # Only include if confidence meets threshold
                        if score >= threshold:
                            local_start = e.get("start", 0)
                            local_end = e.get("end", 0)
                            global_start, global_end = self.calculate_global_position(
                                chunk_offset, local_start, local_end
                            )

                            all_entities.append(
                                {
                                    "text": e.get("text", ""),
                                    "label": label,
                                    "confidence": float(score),
                                    "start": global_start,
                                    "end": global_end,
                                    "chunk_id": chunk_id,
                                }
                            )

                except Exception as e:
                    logger.warning(
                        f"Error extracting entities from chunk {chunk_id}: {e}"
                    )
                    import traceback
                    logger.debug(traceback.format_exc())
                    continue

            # Deduplicate entities if enabled
            if DEDUPLICATION_ENABLED:
                all_entities = self.deduplicate_entities(all_entities)

            # Store in Redis
            entities_key = f"orchestrator:job:{job_id}:entities_raw"
            self.redis_client.set(entities_key, json.dumps(all_entities))

            self.redis_client.hset(
                f"orchestrator:job:{job_id}:steps", "entities", "completed"
            )

            self.event_bus.publish_job_progress(job_id, 66, "entities")

            duration = time.time() - start_time
            job_duration.observe(duration)
            jobs_total.labels(status="success").inc()

            logger.info(
                f"Entities completed for job: {job_id} in {duration:.2f}s, found {len(all_entities)} entities"
            )

            ch.basic_ack(delivery_tag=method.delivery_tag)

        except Exception as e:
            logger.error(f"Error processing entities: {e}")
            jobs_total.labels(status="error").inc()
            if job_id:
                self.redis_client.hset(
                    f"orchestrator:job:{job_id}:status", mapping={"entities": "error"}
                )
                self.event_bus.publish_job_failed(job_id, str(e))
            ch.basic_nack(delivery_tag=method.delivery_tag, requeue=True)


def parse_rabbitmq_url(url: str) -> pika.ConnectionParameters:
    from urllib.parse import urlparse

    parsed = urlparse(url)

    credentials = pika.PlainCredentials(
        parsed.username or "guest", parsed.password or "guest"
    )

    return pika.ConnectionParameters(
        host=parsed.hostname or "localhost",
        port=parsed.port or 5672,
        virtual_host=parsed.path[1:] if parsed.path else "/",
        credentials=credentials,
        heartbeat=600,
        blocked_connection_timeout=300,
    )


@contextmanager
def connect_rabbitmq(url: str, max_retries: int = 5):
    for attempt in range(max_retries):
        try:
            params = parse_rabbitmq_url(url)
            connection = pika.BlockingConnection(params)
            channel = connection.channel()
            prefetch_count = int(os.getenv("PREFETCH_COUNT", "5"))
            channel.basic_qos(prefetch_count=prefetch_count)
            logger.info(
                f"Connected to RabbitMQ at {params.host}:{params.port} with prefetch_count={prefetch_count}"
            )
            yield connection, channel
            return
        except Exception as e:
            logger.warning(
                f"Failed to connect to RabbitMQ (attempt {attempt + 1}/{max_retries}): {e}"
            )
            if attempt < max_retries - 1:
                time.sleep(2**attempt)
    raise Exception("Failed to connect to RabbitMQ after max retries")


def signal_handler(signum, frame):
    logger.info("Received shutdown signal, stopping worker...")
    sys.exit(0)


def main():
    logger.info("Starting Entities Worker")

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    start_http_server(METRICS_PORT)
    logger.info(f"Metrics server started on port {METRICS_PORT}")

    worker = EntitiesWorker()
    worker.load_model()

    while True:
        try:
            with connect_rabbitmq(RABBITMQ_URL) as (connection, channel):
                logger.info(f"Consuming from queue: {QUEUE_NAME}")

                channel.basic_consume(
                    queue=QUEUE_NAME, on_message_callback=worker.process, auto_ack=False
                )

                channel.start_consuming()

        except Exception as e:
            logger.error(f"RabbitMQ connection error: {e}")
            time.sleep(5)


if __name__ == "__main__":
    main()
