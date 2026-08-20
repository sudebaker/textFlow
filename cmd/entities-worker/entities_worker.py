#!/usr/bin/env python3
"""
Entities Worker for textFlow
Consumes messages from RabbitMQ and extracts entities using GLiNER
"""

import os
import sys

os.environ["HF_HUB_OFFLINE"] = "1"
os.environ["TRANSFORMERS_OFFLINE"] = "1"
os.environ["HF_HOME"] = "/home/app/.cache/huggingface"
os.environ["TRANSFORMERS_CACHE"] = "/home/app/.cache/huggingface"
os.environ["HF_DATASETS_CACHE"] = "/home/app/.cache/huggingface"
os.environ["TOKENIZERS_PARALLELISM"] = "false"

sys.path.insert(0, "/app")

import json
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Dict, List, Optional

import requests
from pathlib import Path

from pkg.worker_common.base import BaseWorker
from pkg.worker_common.entity_utils import entity_id
from pkg.worker_common.rabbitmq import parse_rabbitmq_url
from app.config.settings import Settings as AppSettings
from sliding_window import (
    process_with_sliding_window,
    estimate_tokens,
    requires_sliding_window,
)

QUEUE_NAME = os.getenv("QUEUE_NAME", "entities")
INFERENCES_QUEUE = os.getenv("INFERENCES_QUEUE", "inferences")
METRICS_PORT = int(os.getenv("METRICS_PORT", "8002"))

app_settings = AppSettings()
GLINER_MODEL_PATH = app_settings.gliner_model_path
ENTITY_TYPES = os.getenv("ENTITY_TYPES", "PERSON,ORGANIZATION,LOCATION,DATE,MONEY,EMAIL")


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
ENTITY_THRESHOLDS = app_settings.get_threshold_map()


def extract_regex_parallel(
    text: str,
    regex_fn: Optional[Callable[[str], list]],
    gliner_fn: Callable[[], list],
) -> list:
    """Run regex extraction in a background thread concurrent with gliner_fn.

    Returns gliner_fn() results merged with regex results. Degrades silently:
    if regex_fn is None, text is empty, or regex_fn raises, only gliner_fn()
    results are returned.

    Args:
        text: Full document text (regex input). Fetched before dispatch.
        regex_fn: Callable taking text and returning a list of entities.
        gliner_fn: Callable taking no args; runs in the caller thread (GLiNER).

    Returns:
        Merged entity list (gliner results first, then regex results).
    """
    with ThreadPoolExecutor(max_workers=1) as executor:
        regex_future = None
        if text and regex_fn is not None:
            regex_future = executor.submit(regex_fn, text)
        entities = gliner_fn()
        if regex_future is not None:
            try:
                entities.extend(regex_future.result())
            except Exception:
                pass
    return entities


class EntitiesWorker(BaseWorker):
    def __init__(self):
        super().__init__(
            worker_name="entities-worker",
            queue_name=QUEUE_NAME,
            metrics_port=METRICS_PORT,
            requires_gpu=True,
        )
        self.model = None
        self.device = ENTITIES_DEVICE
        self.default_entities = [e.strip() for e in ENTITY_TYPES.split(",")]
        self.regex_enabled = app_settings.regex_enabled
        self.regex_service_url = app_settings.regex_service_url
        self.regex_timeout = app_settings.regex_timeout

    @staticmethod
    def _normalize_entity_types(entity_types) -> list:
        if isinstance(entity_types, list):
            return [str(t).strip().upper() for t in entity_types if t]
        if not entity_types or not str(entity_types).strip():
            return []
        entity_types_str = str(entity_types).strip()
        if entity_types_str.startswith("["):
            try:
                parsed = json.loads(entity_types_str)
                if isinstance(parsed, list):
                    return [str(entry).strip().upper() for entry in parsed if entry]
            except (json.JSONDecodeError, TypeError):
                pass
        return [t.strip().upper() for t in entity_types_str.split(",") if t.strip()]

    def load_model(self):
        from gliner import GLiNER

        model_path = os.getenv("GLINER_MODEL_PATH", "/models/gliner-small-v2.1")
        deberta_path = "/models/deberta-v3-small"

        self.logger.info("Loading GLiNER Model (Offline Mode)")
        self.logger.info(f"   GLiNER model path: {model_path}")
        self.logger.info(f"   DeBERTa backbone path: {deberta_path}")

        try:
            model_path_obj = Path(model_path)
            if not model_path_obj.exists():
                raise FileNotFoundError(f"GLiNER model directory not found: {model_path}")

            config_file = model_path_obj / "gliner_config.json"
            if not config_file.exists():
                raise FileNotFoundError(f"gliner_config.json not found in {model_path}")

            model_files = list(model_path_obj.glob("*.bin")) + list(
                model_path_obj.glob("*.safetensors")
            )
            if not model_files:
                raise FileNotFoundError(
                    f"No model weight files (.bin or .safetensors) found in {model_path}"
                )

            deberta_path_obj = Path(deberta_path)
            if not deberta_path_obj.exists():
                raise FileNotFoundError(
                    f"DeBERTa backbone directory not found at {deberta_path}"
                )

            critical_files = ["spm.model", "tokenizer_config.json", "config.json"]
            missing_files = [f for f in critical_files if not (deberta_path_obj / f).exists()]
            if missing_files:
                raise FileNotFoundError(
                    f"Missing DeBERTa tokenizer files in {deberta_path}: {missing_files}"
                )

            self.model = GLiNER.from_pretrained(
                model_path,
                local_files_only=True,
            )
            self.logger.info("GLiNER model loaded successfully")

            if self.device != "cpu":
                self.model = self.model.to(self.device)
                try:
                    import torch
                    if torch.cuda.is_available():
                        mem_gb = torch.cuda.memory_allocated() / 1e9
                        self.logger.info(f"GPU memory allocated: {mem_gb:.2f} GB")
                except Exception:
                    pass

            self.logger.info(f"Model type: {type(self.model).__name__}, Device: {self.device}")

        except Exception as e:
            self.logger.error(f"Failed to load GLiNER model: {e}")
            raise

    def _extract_dates(self, text: str) -> List[Dict]:
        import re
        dates = []
        patterns = [
            r"\d{1,2}/\d{1,2}/\d{2,4}",
            r"\d{1,2}-\d{1,2}-\d{2,4}",
            r"\d{4}-\d{2}-\d{2}",
            r"(?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2},?\s+\d{4}",
            r"\d{1,2}\s+(?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{4}",
        ]
        for pattern in patterns:
            for match in re.finditer(pattern, text, re.IGNORECASE):
                dates.append({"text": match.group(), "label": "DATE", "confidence": 0.75, "start": match.start(), "end": match.end()})
        return dates

    def _extract_money(self, text: str) -> List[Dict]:
        import re
        money = []
        patterns = [
            r"\$\d+(?:,\d{3})*(?:\.\d{2})?",
            r"\d+(?:,\d{3})*(?:\.\d{2})?\s*(?:USD|EUR|GBP)",
            r"€\d+(?:,\d{3})*(?:\.\d{2})?",
            r"£\d+(?:,\d{3})*(?:\.\d{2})?",
        ]
        for pattern in patterns:
            for match in re.finditer(pattern, text, re.IGNORECASE):
                money.append({"text": match.group(), "label": "MONEY", "confidence": 0.8, "start": match.start(), "end": match.end()})
        return money

    def _extract_orgs(self, text: str) -> List[Dict]:
        import re
        orgs = []
        patterns = [
            r"(?:Inc\.|LLC|Corp\.|Ltd\.|S\.A\.|S\.L\.|B\.V\.|GmbH)",
            r"(?:University|Institute|Foundation|Association|Corporation)",
        ]
        for pattern in patterns:
            for match in re.finditer(pattern, text, re.IGNORECASE):
                orgs.append({"text": match.group(), "label": "ORG", "confidence": 0.6, "start": match.start(), "end": match.end()})
        return orgs

    def _extract_locs(self, text: str) -> List[Dict]:
        import re
        locs = []
        patterns = [
            r"(?:New York|Los Angeles|Chicago|Houston|Phoenix)",
            r"(?:Madrid|Barcelona|Valencia|Sevilla|Málaga|Bilbao)",
            r"(?:Spain|France|Germany|Italy|United Kingdom|United States)",
        ]
        for pattern in patterns:
            for match in re.finditer(pattern, text, re.IGNORECASE):
                locs.append({"text": match.group(), "label": "LOC", "confidence": 0.65, "start": match.start(), "end": match.end()})
        return locs

    def _extract_persons(self, text: str) -> List[Dict]:
        import re
        persons = []
        pattern = r"\b[A-Z][a-z]+\s+[A-Z][a-z]+\b"
        exclude = {"The", "This", "That", "What", "When", "Where", "Which", "Who", "How", "There"}
        for match in re.finditer(pattern, text):
            name = match.group()
            if name not in exclude:
                persons.append({"text": name, "label": "PER", "confidence": 0.5, "start": match.start(), "end": match.end()})
        return persons

    def predict_entities(self, text: str, entity_types: List[str], threshold: float = 0.5) -> List[Dict]:
        try:
            entities = self.model.predict_entities(text, entity_types, threshold=threshold)
            return entities if entities else []
        except Exception as e:
            self.logger.error(f"Error predicting entities: {e}")
            return []

    def _extract_regex_entities(self, text: str) -> list:
        try:
            payload = {"text": text}
            response = requests.post(
                f"{self.regex_service_url}/preprocess",
                json=payload,
                timeout=self.regex_timeout,
            )
            response.raise_for_status()
            data = response.json()
            entities_by_chunk = data.get("entities", {})
            result = []
            for chunk_id_str, entities_in_chunk in entities_by_chunk.items():
                if isinstance(entities_in_chunk, list):
                    for entity in entities_in_chunk:
                        result.append({"text": entity.get("text", ""), "label": entity.get("label", ""), "confidence": 1.0, "start": 0, "end": 0, "chunk_id": chunk_id_str})
            self.logger.info(f"Extracted {len(result)} entities via regex service")
            return result
        except requests.RequestException as e:
            self.logger.warning(f"Regex entity extractor call failed: {e}")
            return []
        except Exception as e:
            self.logger.warning(f"Error processing regex entities: {e}")
            return []

    def calculate_global_position(self, chunk_offset: int, local_start: int, local_end: int) -> tuple:
        return (chunk_offset + local_start, chunk_offset + local_end)

    def process_message(self, message: Dict) -> Any:
        job_id = message.get("job_id")
        chunks = message.get("chunks", [])
        entity_types = self._normalize_entity_types(message.get("entity_types", self.default_entities))

        self.logger.info(f"Processing entities for job: {job_id} with {len(chunks)} chunks")

        if not chunks:
            chunks_json = self.redis_client.get(f"orchestrator:job:{job_id}:chunks")
            if chunks_json:
                chunks = json.loads(chunks_json)
            else:
                self.logger.warning(f"No chunks found in message or Redis for job: {job_id}")
                self.jobs_total.labels(status="no_chunks").inc()
                return {"status": "no_chunks"}

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

        try:
            text = self.redis_client.get(f"orchestrator:job:{job_id}:text")
        except Exception as e:
            self.logger.warning(f"Failed to read document text: {e}")
            text = None

        def gliner_extract() -> list:
            all_entities = []
            for chunk_id, chunk_text, chunk_offset in large_chunks:
                try:
                    def predict_with_thresholds(text, entity_types, threshold=0.1):
                        return self.model.predict_entities(text, entity_types, threshold=threshold)
                    entities_items = process_with_sliding_window(chunk_text, predict_with_thresholds, entity_types, threshold=0.1)
                    for e in entities_items:
                        label = e.get("label", "")
                        score = e.get("score", 0.0)
                        threshold_val = ENTITY_THRESHOLDS.get(label, 0.5)
                        if score >= threshold_val:
                            g_start, g_end = self.calculate_global_position(chunk_offset, e.get("start", 0), e.get("end", 0))
                            all_entities.append({"text": e.get("text", ""), "label": label, "confidence": float(score), "start": g_start, "end": g_end, "chunk_id": chunk_id})
                except Exception as e:
                    self.logger.warning(f"Error extracting entities from large chunk {chunk_id}: {e}")

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
                            threshold_val = ENTITY_THRESHOLDS.get(label, 0.5)
                            if score >= threshold_val:
                                g_start, g_end = self.calculate_global_position(chunk_offset, e.get("start", 0), e.get("end", 0))
                                all_entities.append({"text": e.get("text", ""), "label": label, "confidence": float(score), "start": g_start, "end": g_end, "chunk_id": chunk_id})
                except Exception as e:
                    self.logger.warning(f"Batch prediction failed: {e}")
                    for chunk_id, chunk_text, chunk_offset in batch:
                        try:
                            entities = self.model.predict_entities(chunk_text, entity_types, threshold=0.1)
                            if entities and isinstance(entities[0], list):
                                entities = entities[0]
                            for e in entities:
                                label = e.get("label", "")
                                score = e.get("score", 0.0)
                                threshold_val = ENTITY_THRESHOLDS.get(label, 0.5)
                                if score >= threshold_val:
                                    g_start, g_end = self.calculate_global_position(chunk_offset, e.get("start", 0), e.get("end", 0))
                                    all_entities.append({"text": e.get("text", ""), "label": label, "confidence": float(score), "start": g_start, "end": g_end, "chunk_id": chunk_id})
                        except Exception as inner_e:
                            self.logger.warning(f"Error extracting entities from chunk {chunk_id}: {inner_e}")
            return all_entities

        regex_fn = self._extract_regex_entities if self.regex_enabled else None
        all_entities = extract_regex_parallel(text, regex_fn, gliner_extract)

        entities_key = f"orchestrator:job:{job_id}:entities_raw"
        for ent in all_entities:
            ent["entity_id"] = entity_id(ent.get("label", ""), ent.get("text", ""))
        self.redis_client.set(entities_key, json.dumps(all_entities))
        self.redis_client.hset(f"orchestrator:job:{job_id}:steps", "entities", "completed")
        self.event_bus.publish_job_progress(job_id, 66, "entities")

        try:
            features_json = self.redis_client.get(f"orchestrator:job:{job_id}:features")
            inferences_enabled = False
            if features_json:
                features = json.loads(features_json)
                inferences_enabled = "inferences" in features

            if inferences_enabled and chunks:
                entities_by_chunk = {}
                for entity in all_entities:
                    cid = entity.get("chunk_id")
                    if cid not in entities_by_chunk:
                        entities_by_chunk[cid] = []
                    entities_by_chunk[cid].append(entity)

                valid_chunks = [c for c in chunks if c.get("text", "").strip()]
                if valid_chunks:
                    source_type_json = self.redis_client.get(f"orchestrator:job:{job_id}:source_classification")
                    source_type = "generico"
                    if source_type_json:
                        try:
                            source_data = json.loads(source_type_json)
                            source_type = source_data.get("document_type", "generico")
                        except Exception:
                            pass

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
                        self._publish_to_queue(INFERENCES_QUEUE, inference_msg)

                    self.redis_client.setex(
                        f"orchestrator:job:{job_id}:inferences:remaining",
                        86400,
                        len(valid_chunks),
                    )
        except Exception as e:
            self.logger.error(f"Failed to trigger inferences: {e}", exc_info=True)

        self.logger.info(f"Entities completed for job: {job_id}, found {len(all_entities)} entities")
        return {"status": "success", "job_id": job_id, "entities": len(all_entities)}

    def cleanup(self) -> None:
        super().cleanup()
        if hasattr(self, "model") and self.model is not None:
            del self.model
        try:
            import torch
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except ImportError:
            pass


def main():
    worker = EntitiesWorker()
    worker.load_model()
    worker.run()


if __name__ == "__main__":
    main()
