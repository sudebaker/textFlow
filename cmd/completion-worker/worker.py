#!/usr/bin/env python3
"""Completion Worker: Final aggregator in the textFlow pipeline.

Subscribes to job:events via Redis pub/sub and aggregates results from all
pipeline workers. Finalizes jobs by saving results to /results/{job_id}.json
and sending webhook notifications.
"""

import hashlib
import hmac
import json
import logging
import os
import sys
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

import msgpack
import requests
from prometheus_client import Counter, Histogram
from pydantic_settings import BaseSettings
from rapidfuzz import fuzz
from unidecode import unidecode

os.environ["HF_HUB_OFFLINE"] = "1"
os.environ["TRANSFORMERS_OFFLINE"] = "1"

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))
sys.path.insert(0, os.path.dirname(__file__))
from pkg.worker_common.pubsub_base import BasePubSubWorker

try:
    from sentence_transformers import SentenceTransformer
    import torch
    SENTENCE_TRANSFORMERS_AVAILABLE = True
except ImportError as e:
    logger.warning(f"sentence-transformers not available: {e}")
    SENTENCE_TRANSFORMERS_AVAILABLE = False
    SentenceTransformer = None
    torch = None


class Settings(BaseSettings):
    redis_url: str = "redis://localhost:6379"
    fuzzy_match_threshold: float = 0.85
    webhook_url: str = ""
    results_path: str = "/app/data/results"
    api_base_url: str = "http://localhost:8080"
    metrics_port: int = 8005
    embeddings_model_path: str = "/models/bge-m3"
    embeddings_device: str = "cuda"
    embedding_batch_size: int = 32

    class Config:
        env_prefix = ""


settings = Settings()

jobs_finalized_total = Counter(
    "completion_worker_jobs_finalized_total", "Total jobs finalized", ["status"]
)
job_finalization_duration = Histogram(
    "completion_worker_job_finalization_duration_seconds",
    "Job finalization duration in seconds",
)


class CompletionWorker(BasePubSubWorker):
    def __init__(self):
        super().__init__(
            worker_name="completion-worker",
            metrics_port=settings.metrics_port,
        )
        self.default_required_steps = {
            "extraction",
            "embeddings",
            "entities",
            "metadata",
        }
        self.spreadsheet_required_steps = {"extraction", "entities"}
        self._embedding_service = None
        self._embedding_service_loaded = False

    def _get_embedding_service(self):
        if self._embedding_service_loaded:
            return self._embedding_service
        if not SENTENCE_TRANSFORMERS_AVAILABLE:
            logger.warning("sentence-transformers not available, inference embeddings disabled")
            self._embedding_service_loaded = True
            return None
        try:
            device = settings.embeddings_device if torch.cuda.is_available() else "cpu"
            self._embedding_service = SentenceTransformer(
                settings.embeddings_model_path,
                device=device,
            )
            logger.info(f"Embedding service loaded with device={device}")
        except Exception as e:
            logger.warning(f"Failed to load embedding service: {e}")
            self._embedding_service = None
        self._embedding_service_loaded = True
        return self._embedding_service

    def _generate_inference_embeddings(
        self, inferences_by_chunk: Dict[str, List[Dict]]
    ) -> Dict[str, Dict[str, List[float]]]:
        if not inferences_by_chunk:
            return {}
        inference_embeddings: Dict[str, Dict[str, List[float]]] = {}
        for chunk_id, inferences in inferences_by_chunk.items():
            if not inferences:
                continue
            texts = [inf.get("text", "") or "" for inf in inferences]
            if not any(texts):
                continue
            try:
                embeddings = self._get_embedding_service().encode(
                    texts,
                    batch_size=settings.embedding_batch_size,
                    show_progress_bar=False,
                    convert_to_numpy=True,
                )
                chunk_embeddings: Dict[str, List[float]] = {}
                for idx, embedding in enumerate(embeddings):
                    chunk_embeddings[f"inference_{idx}"] = (
                        embedding.tolist() if hasattr(embedding, "tolist") else list(embedding)
                    )
                inference_embeddings[chunk_id] = chunk_embeddings
            except Exception as e:
                logger.warning(f"Failed to generate embeddings for chunk {chunk_id}: {e}")
        return inference_embeddings

    def save_results_to_file(self, job_id: str, results: Dict[str, Any]) -> bool:
        try:
            os.makedirs(settings.results_path, exist_ok=True)
            file_path = os.path.join(settings.results_path, f"{job_id}.json")
            temp_path = file_path + ".tmp"
            with open(temp_path, "w", encoding="utf-8") as f:
                json.dump(results, f, ensure_ascii=False, indent=2)
            os.rename(temp_path, file_path)
            logger.info(f"Results saved to {file_path}")
            return True
        except Exception as e:
            logger.error(f"Failed to save results to file: {e}")
            return False

    def send_webhook(
        self, job_id: str, status: str, error: Optional[str] = None
    ) -> bool:
        webhook_url = settings.webhook_url
        webhook_secret = ""
        if job_id:
            meta = self.redis_client.hgetall(f"orchestrator:job:{job_id}:meta")
            job_webhook_url = meta.get("webhook_url", "")
            if job_webhook_url:
                webhook_url = job_webhook_url
                webhook_secret = meta.get("webhook_secret", "")
        if not webhook_url:
            return False
        try:
            payload = {
                "job_id": job_id,
                "status": status,
                "download_url": f"{settings.api_base_url}/v1/documents/{job_id}/download",
                "completed_at": datetime.utcnow().isoformat() + "Z",
            }
            if error:
                payload["error"] = error
            headers = {"Content-Type": "application/json"}
            if webhook_secret:
                timestamp = str(int(time.time()))
                payload_json = json.dumps(payload, sort_keys=True)
                signature = hmac.new(
                    webhook_secret.encode(),
                    payload_json.encode(),
                    hashlib.sha256,
                ).hexdigest()
                headers["X-Webhook-Signature"] = signature
                headers["X-Webhook-Timestamp"] = timestamp
            response = requests.post(
                webhook_url,
                json=payload,
                timeout=10,
                headers=headers,
            )
            response.raise_for_status()
            logger.info(f"Webhook sent successfully for job {job_id}")
            return True
        except Exception as e:
            logger.error(f"Failed to send webhook: {e}")
            return False

    def _check_and_notify_batch(self, job_id: str, status: str):
        batch_id = self.redis_client.hget(
            f"orchestrator:job:{job_id}:meta", "batch_id"
        )
        if not batch_id:
            return
        batch_id = batch_id.decode() if isinstance(batch_id, bytes) else batch_id
        batch_done_key = f"orchestrator:batch:{batch_id}:done_count"
        done = self.redis_client.incr(batch_done_key)
        if done == 1:
            self.redis_client.expire(batch_done_key, 24 * 60 * 60)
        total = int(
            self.redis_client.hget(f"orchestrator:batch:{batch_id}:meta", "total") or 0
        )
        if done >= total:
            webhook_url = self.redis_client.hget(
                f"orchestrator:batch:{batch_id}:meta", "webhook_url"
            )
            webhook_secret = self.redis_client.hget(
                f"orchestrator:batch:{batch_id}:meta", "webhook_secret"
            )
            if webhook_url:
                webhook_url = (
                    webhook_url.decode() if isinstance(webhook_url, bytes) else webhook_url
                )
                webhook_secret = (
                    webhook_secret.decode() if isinstance(webhook_secret, bytes) else webhook_secret
                )
                self._send_batch_webhook(batch_id, status, webhook_url, webhook_secret)

    def _send_batch_webhook(
        self,
        batch_id: str,
        final_status: str,
        webhook_url: str,
        webhook_secret: Optional[str] = None,
    ):
        try:
            jobs = self.redis_client.smembers(f"orchestrator:batch:{batch_id}:jobs")
            job_statuses = []
            completed = failed = 0
            for job_id in jobs:
                job_id = job_id.decode() if isinstance(job_id, bytes) else job_id
                if not job_id:
                    continue
                status = self.redis_client.hget(
                    f"orchestrator:job:{job_id}:status", "status"
                )
                status = status.decode() if isinstance(status, bytes) else status
                job_statuses.append({"id": job_id, "status": status})
                if status == "completed":
                    completed += 1
                else:
                    failed += 1
            if failed == len(jobs):
                batch_status = "failed"
            elif failed > 0:
                batch_status = "partial"
            else:
                batch_status = "completed"
            payload = {
                "batch_id": batch_id,
                "status": batch_status,
                "total": len(jobs),
                "completed": completed,
                "failed": failed,
                "jobs": job_statuses,
            }
            headers = {"Content-Type": "application/json"}
            if webhook_secret:
                signature = hmac.new(
                    webhook_secret.encode(),
                    json.dumps(payload).encode(),
                    hashlib.sha256,
                ).hexdigest()
                headers["X-Webhook-Signature"] = f"sha256={signature}"
            response = requests.post(webhook_url, json=payload, timeout=10, headers=headers)
            response.raise_for_status()
            logger.info(f"Batch webhook sent for {batch_id}")
        except Exception as e:
            logger.error(f"Failed to send batch webhook: {e}")

    def deduplicate_entities(self, entities: list) -> dict:
        if not entities:
            return {}

        def _normalize(text: str) -> str:
            return unidecode(text).lower().strip()

        def _generate_id(label: str, text: str) -> str:
            key = f"{label}:{_normalize(text)}"
            return hashlib.sha256(key.encode()).hexdigest()[:12]

        result: dict = {}
        norm_index: dict = {}

        for ent in entities:
            label = ent.get("label", "")
            text = ent.get("text", "")
            confidence = ent.get("confidence", 0.0)
            norm_text = _normalize(text)
            matched_id = None
            for existing_id, existing_norm in norm_index.items():
                if result[existing_id]["label"] != label:
                    continue
                similarity = fuzz.ratio(norm_text, existing_norm) / 100.0
                if similarity >= settings.fuzzy_match_threshold:
                    matched_id = existing_id
                    break
            if matched_id:
                if confidence > result[matched_id].get("confidence", 0):
                    result[matched_id] = {
                        "label": label,
                        "text": text,
                        "confidence": confidence,
                        "start_offset": ent.get("start", 0),
                        "end_offset": ent.get("end", 0),
                        "chunk_id": ent.get("chunk_id", ""),
                    }
                    norm_index[matched_id] = _normalize(text)
            else:
                eid = ent.get("entity_id") or _generate_id(label, text)
                result[eid] = {
                    "label": label,
                    "text": text,
                    "confidence": confidence,
                    "start_offset": ent.get("start", 0),
                    "end_offset": ent.get("end", 0),
                    "chunk_id": ent.get("chunk_id", ""),
                }
                norm_index[eid] = norm_text

        logger.info(
            f"Deduplicated entities: {len(entities)} raw → {len(result)} unique "
            f"(threshold={settings.fuzzy_match_threshold})"
        )
        return result

    def get_job_creation_time(self, job_id: str) -> Optional[str]:
        meta = self.redis_client.hgetall(f"orchestrator:job:{job_id}:meta")
        created_at = meta.get("created_at")
        if created_at:
            return datetime.fromtimestamp(int(created_at)).isoformat() + "Z"
        return None

    def check_job_completion(self, job_id: str):
        try:
            steps = self.redis_client.hgetall(f"orchestrator:job:{job_id}:steps")
            completed_steps = set()
            for step, status in steps.items():
                if status == "completed":
                    completed_steps.add(step)
            logger.info(f"Job {job_id} completed steps: {completed_steps}")
            document_metadata_json = self.redis_client.get(
                f"orchestrator:job:{job_id}:metadata:document"
            )
            document_metadata = (
                json.loads(document_metadata_json) if document_metadata_json else {}
            )
            mime_type = document_metadata.get("mime_type", "")
            is_spreadsheet = "spreadsheet" in mime_type.lower() or mime_type in [
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                "application/vnd.ms-excel",
                "text/csv",
                "application/zip",
            ]
            is_audio = "audio" in completed_steps
            required_steps = (
                self.spreadsheet_required_steps
                if is_spreadsheet
                else self.default_required_steps.copy()
            )
            if is_audio and "extraction" in required_steps:
                required_steps.discard("extraction")
                required_steps.add("audio")
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

    def finalize_job(self, job_id: str):
        finalization_start_time = time.time()
        try:
            logger.info(f"Finalizing job: {job_id}")
            pipe = self.redis_client.pipeline()
            pipe.hgetall(f"orchestrator:job:{job_id}:meta")
            pipe.hgetall(f"orchestrator:job:{job_id}:status")
            pipe.get(f"orchestrator:job:{job_id}:text")
            pipe.get(f"orchestrator:job:{job_id}:metadata:document")
            pipe.get(f"orchestrator:job:{job_id}:metadata:text")
            pipe.get(f"orchestrator:job:{job_id}:chunks")
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
                entities_raw_json,
                source_classification_json,
                micro_inferences_json,
            ) = pipe.execute()
            embeddings_raw_bytes = self.redis_raw.get(
                f"orchestrator:job:{job_id}:embeddings"
            )
            inference_embeddings_raw = self.redis_raw.get(
                f"orchestrator:job:{job_id}:inference_embeddings"
            )
            created_at_timestamp = int(meta.get("created_at", time.time()))
            created_at = datetime.fromtimestamp(created_at_timestamp).isoformat() + "Z"
            completed_at = datetime.fromtimestamp(int(time.time())).isoformat() + "Z"
            if status_data and status_data.get("status") == "completed":
                logger.info(f"Job {job_id} already finalized, skipping")
                return
            text = text or ""
            document_metadata = (
                json.loads(document_metadata_json) if document_metadata_json else {}
            )
            text_metadata = json.loads(text_metadata_json) if text_metadata_json else {}
            chunks = json.loads(chunks_json) if chunks_json else []
            embeddings_by_chunk: dict = {}
            if embeddings_raw_bytes:
                raw = msgpack.unpackb(embeddings_raw_bytes, raw=False)
                embeddings_by_chunk = {k: v for k, v in raw.items() if isinstance(v, list)}
            inference_embeddings_by_chunk: dict = {}
            if inference_embeddings_raw:
                raw = msgpack.unpackb(inference_embeddings_raw, raw=False)
                inference_embeddings_by_chunk = {
                    k: v for k, v in raw.items() if isinstance(v, dict)
                }
            entities_raw = json.loads(entities_raw_json) if entities_raw_json else []
            entities_dict = self.deduplicate_entities(entities_raw) if entities_raw else {}
            logger.info(
                f"Entities: {len(entities_raw)} raw → {len(entities_dict)} unique (by entity_id)"
            )
            entity_ids_by_chunk: dict = {}
            for ent in entities_raw:
                cid = ent.get("chunk_id")
                eid = ent.get("entity_id")
                if cid and eid:
                    entity_ids_by_chunk.setdefault(cid, [])
                    if eid not in entity_ids_by_chunk[cid]:
                        entity_ids_by_chunk[cid].append(eid)
            inferences_by_chunk: dict = {}
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
            except json.JSONDecodeError as e:
                logger.warning(f"Failed to parse micro_inferences JSON: {e}")
            if (
                not inference_embeddings_by_chunk
                and inferences_by_chunk
                and self._get_embedding_service()
            ):
                logger.info(f"Generating inference embeddings for job: {job_id}")
                inference_embeddings_by_chunk = self._generate_inference_embeddings(
                    inferences_by_chunk
                )
                if inference_embeddings_by_chunk:
                    try:
                        key = f"orchestrator:job:{job_id}:inference_embeddings"
                        packed = msgpack.packb(
                            inference_embeddings_by_chunk, use_bin_type=True
                        )
                        self.redis_raw.set(key, packed)
                    except Exception as e:
                        logger.warning(
                            f"Failed to save inference embeddings to Redis: {e}"
                        )
            enriched_chunks = []
            for chunk in chunks:
                cid = chunk.get("chunk_id", "")
                enriched = dict(chunk)
                enriched["embeddings"] = embeddings_by_chunk.get(cid, [])
                enriched["entity_ids"] = entity_ids_by_chunk.get(cid, [])
                inferences = inferences_by_chunk.get(cid, [])
                chunk_inf_emb = inference_embeddings_by_chunk.get(cid, {})
                for idx, inf in enumerate(inferences):
                    inf_copy = dict(inf)
                    emb_key = f"inference_{idx}"
                    if emb_key in chunk_inf_emb:
                        inf_copy["embedding"] = chunk_inf_emb[emb_key]
                    inferences[idx] = inf_copy
                enriched["inferences"] = inferences
                enriched_chunks.append(enriched)
            results = {
                "job_id": job_id,
                "status": "completed",
                "created_at": created_at,
                "completed_at": completed_at,
                "text": text,
                "document_metadata": document_metadata,
                "text_metadata": text_metadata,
                "chunks": enriched_chunks,
                "entities": entities_dict,
            }
            if source_classification is not None:
                results["source_classification"] = source_classification
            total_inferences = sum(len(c.get("inferences", [])) for c in enriched_chunks)
            log_message = (
                f"Job {job_id} finalized: chunks={len(enriched_chunks)}, "
                f"entities={len(entities_dict)}, inferences={total_inferences}"
            )
            if source_classification:
                log_message += (
                    f", source_type={source_classification.get('document_type', 'unknown')}"
                )
            logger.info(log_message)
            self.redis_client.set(
                f"orchestrator:job:{job_id}:results",
                json.dumps(results, ensure_ascii=False),
            )
            self.redis_client.hset(
                f"orchestrator:job:{job_id}:meta",
                "completed_at",
                str(int(time.time())),
            )
            self.save_results_to_file(job_id, results)
            self.redis_client.hset(
                f"orchestrator:job:{job_id}:status", "status", "completed"
            )
            self.send_webhook(job_id, "completed", None)
            self._check_and_notify_batch(job_id, "completed")
            self.event_bus.publish_job_completed(job_id)
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
            job_finalization_duration.observe(time.time() - finalization_start_time)
            jobs_finalized_total.labels(status="error").inc()

    def handle_event(self, message: Dict) -> None:
        try:
            if message["type"] != "message":
                return
            event = json.loads(message["data"])
            event_type = event.get("event_type")
            job_id = event.get("job_id")
            logger.info(f"Received event: {event_type} for job {job_id}")
            if event_type == "job_progress" and job_id:
                self.check_job_completion(job_id)
        except Exception as e:
            logger.error(f"Error handling event: {e}")


if __name__ == "__main__":
    CompletionWorker().start()
