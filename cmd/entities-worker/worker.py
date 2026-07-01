#!/usr/bin/env python3
"""
Entities Worker for textFlow
 Consumes messages from RabbitMQ and extracts entities using GLiNER
"""

import hashlib


def entity_id(label: str, text: str) -> str:
    """Return a stable 12-char hex ID for a (label, text) pair."""
    key = f"{label}:{text.lower().strip()}"
    return hashlib.sha256(key.encode()).hexdigest()[:12]


import os
import sys

os.environ["HF_HUB_OFFLINE"] = "1"
os.environ["TRANSFORMERS_OFFLINE"] = "1"
os.environ["HF_HOME"] = "/home/app/.cache/huggingface"
os.environ["TRANSFORMERS_CACHE"] = "/home/app/.cache/huggingface"
os.environ["HF_DATASETS_CACHE"] = "/home/app/.cache/huggingface"
os.environ["TOKENIZERS_PARALLELISM"] = "false"

import json
import logging
import time
import hashlib
from pathlib import Path
from typing import Dict, List, Any

import pika
import requests
from prometheus_client import Counter

sys.path.insert(0, "/app")
from pkg.worker_common.base import BaseWorker
from pkg.worker_common.rabbitmq import parse_rabbitmq_url
from sliding_window import (
    process_with_sliding_window,
    estimate_tokens,
    requires_sliding_window,
)
from unidecode import unidecode
from rapidfuzz import fuzz

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379")
RABBITMQ_URL = os.getenv("RABBITMQ_URL", "amqp://rabbitmq:5672/")
QUEUE_NAME = os.getenv("QUEUE_NAME", "entities")
INFERENCES_QUEUE = os.getenv("INFERENCES_QUEUE", "inferences")
METRICS_PORT = int(os.getenv("METRICS_PORT", "8002"))
GLINER_MODEL_PATH = os.getenv("GLINER_MODEL_PATH", "/models/gliner-small-v2.1")
ENTITY_TYPES = os.getenv("ENTITY_TYPES", "PERSON,ORGANIZATION,LOCATION,DATE,MONEY,EMAIL")
DEDUPLICATION_ENABLED = os.getenv("DEDUPLICATION_ENABLED", "true").lower() == "true"
FUZZY_MATCH_THRESHOLD = float(os.getenv("FUZZY_MATCH_THRESHOLD", "0.85"))
REGEX_ENTITY_EXTRACTOR_URL = os.getenv(
    "REGEX_ENTITY_EXTRACTOR_URL", "http://regex-entity-extractor:8081"
)


def _resolve_device() -> str:
    device_param = os.getenv("ENTITIES_DEVICE", "").strip().lower()
    if device_param:
        return device_param
    try:
        import torch
        return "cuda" if torch.cuda.is_available() else "cpu"
    except ImportError:
        return "cpu"


ENTITIES_DEVICE = _resolve_device()


class EntitiesWorker(BaseWorker):
    def __init__(self):
        super().__init__(
            worker_name="entities-worker",
            queue_name=QUEUE_NAME,
            metrics_port=METRICS_PORT,
            requires_gpu=True,
        )
        self.default_entities = [e.strip() for e in ENTITY_TYPES.split(",")]
        self.entities_deduplicated = Counter(
            "entities_worker_deduplicated_total", "Total entities deduplicated"
        )
        self.model = None
        self.device = ENTITIES_DEVICE
        self._load_model()

    def _load_model(self):
        from gliner import GLiNER

        deberta_path = "/models/deberta-v3-small"

        logger.info("=" * 70)
        logger.info("🔍 Loading GLiNER Model (Offline Mode)")
        logger.info("=" * 70)
        logger.info(f"   GLiNER model path: {GLINER_MODEL_PATH}")
        logger.info(f"   DeBERTa backbone path: {deberta_path}")

        model_path_obj = Path(GLINER_MODEL_PATH)
        if not model_path_obj.exists():
            raise FileNotFoundError(f"GLiNER model directory not found: {GLINER_MODEL_PATH}")

        config_file = model_path_obj / "gliner_config.json"
        if not config_file.exists():
            raise FileNotFoundError(f"gliner_config.json not found in {GLINER_MODEL_PATH}")

        model_files = list(model_path_obj.glob("*.bin")) + list(model_path_obj.glob("*.safetensors"))
        if not model_files:
            raise FileNotFoundError(f"No model weight files in {GLINER_MODEL_PATH}")

        deberta_path_obj = Path(deberta_path)
        if not deberta_path_obj.exists():
            raise FileNotFoundError(f"DeBERTa backbone not found at {deberta_path}")

        critical_files = ["spm.model", "tokenizer_config.json", "config.json"]
        missing = [f for f in critical_files if not (deberta_path_obj / f).exists()]
        if missing:
            raise FileNotFoundError(f"Missing DeBERTa files in {deberta_path}: {missing}")

        logger.info("\n🚀 Loading GLiNER...")
        self.model = GLiNER.from_pretrained(GLINER_MODEL_PATH, local_files_only=True)
        if self.device != "cpu":
            self.model = self.model.to(self.device)
            try:
                import torch
                if torch.cuda.is_available():
                    mem_gb = torch.cuda.memory_allocated() / 1e9
                    logger.info(f"   GPU memory allocated: {mem_gb:.2f} GB")
            except Exception:
                pass

        logger.info("✅ GLiNER Model Loaded Successfully")
        logger.info(f"   Device: {self.device}")

    def _normalize_entity_types(self, entity_types) -> List[str]:
        if isinstance(entity_types, list):
            return [str(t).strip().upper() for t in entity_types if t]
        if not entity_types or not str(entity_types).strip():
            return []
        entity_types_str = str(entity_types).strip()
        if entity_types_str.startswith("["):
            try:
                parsed = json.loads(entity_types_str)
                if isinstance(parsed, list):
                    return [str(e).strip().upper() for e in parsed if e]
            except (json.JSONDecodeError, TypeError):
                pass
        return [t.strip().upper() for t in entity_types_str.split(",") if t.strip()]

    def _extract_regex_entities(self, text: str) -> List[Dict]:
        try:
            resp = requests.post(
                f"{REGEX_ENTITY_EXTRACTOR_URL}/preprocess",
                json={"text": text},
                timeout=30,
            )
            resp.raise_for_status()
            data = resp.json()
            entities_by_chunk = data.get("entities", {})
            result = []
            for chunk_id_str, entities_in_chunk in entities_by_chunk.items():
                if isinstance(entities_in_chunk, list):
                    for entity in entities_in_chunk:
                        result.append({
                            "text": entity.get("text", ""),
                            "label": entity.get("label", ""),
                            "confidence": 1.0,
                            "start": 0,
                            "end": 0,
                            "chunk_id": chunk_id_str,
                        })
            logger.info(f"Extracted {len(result)} entities via regex service")
            return result
        except Exception as e:
            logger.warning(f"Regex entity extractor failed: {e}. Continuing without regex entities.")
            return []

    def _calculate_global_position(self, chunk_offset: int, local_start: int, local_end: int) -> tuple:
        return (chunk_offset + local_start, chunk_offset + local_end)

    def _normalize_entity_text(self, text: str) -> str:
        return unidecode(text).lower().strip()

    def _deduplicate_entities(self, entities: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        if not DEDUPLICATION_ENABLED or not entities:
            return entities

        deduplicated = {}
        original_count = len(entities)

        for entity in entities:
            text = entity["text"]
            label = entity["label"]
            normalized_key = f"{label}:{self._normalize_entity_text(text)}"

            found_similar = False
            for existing_key, existing_entity in deduplicated.items():
                if existing_key.startswith(f"{label}:"):
                    similarity = fuzz.ratio(
                        self._normalize_entity_text(text),
                        self._normalize_entity_text(existing_entity["text"]),
                    ) / 100.0
                    if similarity >= FUZZY_MATCH_THRESHOLD:
                        if entity.get("confidence", 0) > existing_entity.get("confidence", 0):
                            existing_entity["confidence"] = entity["confidence"]
                            existing_entity["text"] = text
                        if "positions" not in existing_entity:
                            existing_entity["positions"] = []
                        existing_entity["positions"].append({
                            "chunk_id": entity.get("chunk_id"),
                            "start": entity.get("start"),
                            "end": entity.get("end"),
                        })
                        found_similar = True
                        break

            if not found_similar:
                deduplicated[normalized_key] = entity.copy()
                deduplicated[normalized_key]["positions"] = []

        result = list(deduplicated.values())
        if len(result) < original_count:
            dedup_count = original_count - len(result)
            logger.info(f"Deduplicated {dedup_count} entities ({original_count} -> {len(result)})")
            self.entities_deduplicated.inc(dedup_count)

        return result

    def process_message(self, message: Dict) -> Dict:
        job_id = message.get("job_id")
        chunks = message.get("chunks", [])

        entity_types = self._normalize_entity_types(message.get("entity_types", self.default_entities))
        logger.info(f"Processing entities for job: {job_id} with {len(chunks)} chunks")
        logger.info(f"Entity types: {entity_types}")

        if not chunks:
            chunks_json = self.redis_client.get(f"orchestrator:job:{job_id}:chunks")
            if chunks_json:
                chunks = json.loads(chunks_json)
            else:
                raise ValueError(f"No chunks found in message or Redis for job: {job_id}")

        all_entities = []

        GLINER_BATCH_SIZE = int(os.getenv("GLINER_BATCH_SIZE", "32"))
        batch_chunks = []
        large_chunks = []

        for chunk in chunks:
            chunk_id = chunk.get("chunk_id")
            chunk_text = chunk.get("text", "")
            chunk_offset = chunk.get("start_offset") or chunk.get("offset") or 0
            if not chunk_text:
                continue
            if requires_sliding_window(chunk_text):
                large_chunks.append((chunk_id, chunk_text, chunk_offset))
            else:
                batch_chunks.append((chunk_id, chunk_text, chunk_offset))

        for chunk_id, chunk_text, chunk_offset in large_chunks:
            estimated_tokens = estimate_tokens(chunk_text)
            logger.info(f"Chunk {chunk_id}: {estimated_tokens} tokens, sliding window")
            try:
                def predict_with_thresholds(text, entity_types, threshold=0.1):
                    return self.model.predict_entities(text, entity_types, threshold=threshold)

                entities_items = process_with_sliding_window(
                    chunk_text, predict_with_thresholds, entity_types, threshold=0.1
                )
                for e in entities_items:
                    label = e.get("label", "")
                    score = e.get("score", 0.0)
                    threshold_val = 0.5
                    if score >= threshold_val:
                        g_start, g_end = self._calculate_global_position(
                            chunk_offset, e.get("start", 0), e.get("end", 0)
                        )
                        all_entities.append({
                            "text": e.get("text", ""),
                            "label": label,
                            "confidence": float(score),
                            "start": g_start,
                            "end": g_end,
                            "chunk_id": chunk_id,
                        })
            except Exception as e:
                logger.warning(f"Error extracting entities from large chunk {chunk_id}: {e}")

        for batch_start in range(0, len(batch_chunks), GLINER_BATCH_SIZE):
            batch = batch_chunks[batch_start:batch_start + GLINER_BATCH_SIZE]
            texts = [c[1] for c in batch]
            try:
                batch_predictions = self.model.predict_entities(texts, entity_types, threshold=0.1)
                for (chunk_id, chunk_text, chunk_offset), entities_items in zip(batch, batch_predictions):
                    if entities_items and isinstance(entities_items[0], list):
                        entities_items = entities_items[0]
                    for e in entities_items:
                        label = e.get("label", "")
                        score = e.get("score", 0.0)
                        if score >= 0.5:
                            g_start, g_end = self._calculate_global_position(
                                chunk_offset, e.get("start", 0), e.get("end", 0)
                            )
                            all_entities.append({
                                "text": e.get("text", ""),
                                "label": label,
                                "confidence": float(score),
                                "start": g_start,
                                "end": g_end,
                                "chunk_id": chunk_id,
                            })
            except Exception as e:
                logger.warning(f"Batch prediction failed: {e}. Falling back to individual.")
                for chunk_id, chunk_text, chunk_offset in batch:
                    try:
                        entities = self.model.predict_entities(chunk_text, entity_types, threshold=0.1)
                        if entities and isinstance(entities[0], list):
                            entities = entities[0]
                        for e in entities:
                            label = e.get("label", "")
                            score = e.get("score", 0.0)
                            if score >= 0.5:
                                g_start, g_end = self._calculate_global_position(
                                    chunk_offset, e.get("start", 0), e.get("end", 0)
                                )
                                all_entities.append({
                                    "text": e.get("text", ""),
                                    "label": label,
                                    "confidence": float(score),
                                    "start": g_start,
                                    "end": g_end,
                                    "chunk_id": chunk_id,
                                })
                    except Exception as inner_e:
                        logger.warning(f"Error extracting entities from chunk {chunk_id}: {inner_e}")

        logger.info(
            f"Batch entity extraction: {len(batch_chunks)} small chunks, "
            f"{len(large_chunks)} large chunks, {len(all_entities)} entities found"
        )

        try:
            text = self.redis_client.get(f"orchestrator:job:{job_id}:text")
            if text:
                regex_entities = self._extract_regex_entities(text)
                all_entities.extend(regex_entities)
        except Exception as e:
            logger.warning(f"Failed to extract regex entities: {e}")

        if DEDUPLICATION_ENABLED:
            all_entities = self._deduplicate_entities(all_entities)

        for ent in all_entities:
            ent["entity_id"] = entity_id(ent.get("label", ""), ent.get("text", ""))

        entities_key = f"orchestrator:job:{job_id}:entities_raw"
        self.redis_client.set(entities_key, json.dumps(all_entities))

        self.redis_client.hset(f"orchestrator:job:{job_id}:steps", "entities", "completed")

        self.event_bus.publish_job_progress(job_id, 66, "entities")

        self._publish_inference_tasks(job_id, chunks, all_entities)

        logger.info(f"Entities completed for job: {job_id}, found {len(all_entities)} entities")
        return {"entities": all_entities, "count": len(all_entities)}

    def _publish_inference_tasks(self, job_id: str, chunks: List[Dict], entities: List[Dict]) -> None:
        try:
            features_json = self.redis_client.get(f"orchestrator:job:{job_id}:features")
            inferences_enabled = False
            if features_json:
                features = json.loads(features_json)
                inferences_enabled = "inferences" in features

            valid_chunks = [c for c in chunks if c.get("text", "").strip()]
            if not inferences_enabled or not valid_chunks:
                return

            entities_by_chunk = {}
            for entity in entities:
                cid = entity.get("chunk_id")
                if cid not in entities_by_chunk:
                    entities_by_chunk[cid] = []
                entities_by_chunk[cid].append(entity)

            source_type = "generico"
            source_type_json = self.redis_client.get(f"orchestrator:job:{job_id}:source_classification")
            if source_type_json:
                try:
                    source_data = json.loads(source_type_json)
                    source_type = source_data.get("document_type", "generico")
                except Exception:
                    pass

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
                        "total_chunks": len(valid_chunks),
                    }
                    channel.basic_publish(
                        exchange="",
                        routing_key=INFERENCES_QUEUE,
                        body=json.dumps(inference_msg),
                        properties=pika.BasicProperties(delivery_mode=2),
                    )
                self.redis_client.setex(
                    f"orchestrator:job:{job_id}:inferences:remaining",
                    86400,
                    len(valid_chunks),
                )
                logger.info(f"Published {len(valid_chunks)} inference tasks for job {job_id}")
            finally:
                connection.close()
        except Exception as e:
            logger.error(f"Failed to trigger inferences: {e}", exc_info=True)


if __name__ == "__main__":
    worker = EntitiesWorker()
    worker.run()
