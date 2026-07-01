#!/usr/bin/env python3
"""
Embeddings Worker for textFlow
Consumes messages from RabbitMQ and generates embeddings for each chunk using BAAI/bge-m3

⭐ GPU Features:
- Automatic GPU detection via torch.cuda
- Adaptive batching (GPU=32, CPU=2)
- FP16 optimization for GPU (in EmbeddingService)
"""

import os
import sys

os.environ["HF_HUB_OFFLINE"] = "1"
os.environ["TRANSFORMERS_OFFLINE"] = "1"

import json
import logging
import time
from typing import Dict, List, Any

import msgpack
import torch
from prometheus_client import Gauge

sys.path.insert(0, "/app")
from pkg.worker_common.base import BaseWorker
from app.services.embeddings import EmbeddingService

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379")
QUEUE_NAME = os.getenv("QUEUE_NAME", "embeddings")
METRICS_PORT = int(os.getenv("METRICS_PORT", "8001"))
MODEL_PATH = os.getenv("MODEL_PATH", "/models/bge-m3")
EMBEDDING_BATCH_SIZE_GPU = int(os.getenv("EMBEDDING_BATCH_SIZE_GPU", "32"))
EMBEDDING_BATCH_SIZE_CPU = int(os.getenv("EMBEDDING_BATCH_SIZE_CPU", "2"))

_device_env = os.getenv("EMBEDDINGS_DEVICE", "").strip()
EMBEDDINGS_DEVICE = _device_env if _device_env else None


def _detect_gpu() -> bool:
    if torch is None:
        return False
    return torch.cuda.is_available()


class EmbeddingsWorker(BaseWorker):
    def __init__(self):
        super().__init__(
            worker_name="embeddings-worker",
            queue_name=QUEUE_NAME,
            metrics_port=METRICS_PORT,
            requires_gpu=True,
        )
        self.service = None
        self.batch_size = EMBEDDING_BATCH_SIZE_CPU
        self.gpu_memory_gb = Gauge(
            "embeddings_worker_gpu_memory_gb", "GPU memory usage in GB"
        )
        self._load_model()

    def _load_model(self):
        use_gpu = _detect_gpu()
        self.batch_size = (
            EMBEDDING_BATCH_SIZE_GPU if use_gpu else EMBEDDING_BATCH_SIZE_CPU
        )
        self.gpu_available.labels(device="cuda:0").set(1 if use_gpu else 0)

        if use_gpu:
            gpu_name = torch.cuda.get_device_name() if torch.cuda.is_available() else "Unknown"
            logger.info(f"🚀 GPU Mode detected: {gpu_name}")
            logger.info(f"   Batch size: {self.batch_size} (optimized for GPU)")
        else:
            logger.info(f"📝 CPU Mode detected")
            logger.info(f"   Batch size: {self.batch_size} (conservative for CPU)")

        logger.info(f"Loading embeddings model from: {MODEL_PATH}")
        self.service = EmbeddingService(model_path=MODEL_PATH, device=EMBEDDINGS_DEVICE)
        logger.info("✅ Embeddings model loaded successfully")

    def _check_micro_inferences_exist(self, job_id: str) -> bool:
        key = f"orchestrator:job:{job_id}:micro_inferences"
        return self.redis_client.exists(key) > 0

    def _load_micro_inferences(self, job_id: str) -> List[Dict[str, Any]]:
        key = f"orchestrator:job:{job_id}:micro_inferences"
        raw = self.redis_client.get(key)
        if not raw:
            return []
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            logger.warning(f"Failed to parse micro_inferences for job: {job_id}")
            return []

    def _generate_inference_embeddings(
        self, micro_inferences: List[Dict[str, Any]]
    ) -> Dict[str, Dict[str, List[float]]]:
        if not micro_inferences:
            return {}

        inference_embeddings: Dict[str, Dict[str, List[float]]] = {}

        for chunk_data in micro_inferences:
            chunk_id = chunk_data.get("chunk_id")
            inferences = chunk_data.get("inferences", [])
            if not chunk_id or not inferences:
                continue

            inference_texts = [inf.get("text") or "" for inf in inferences]
            if not any(inference_texts):
                continue

            embeddings = self.service.generate_embeddings(
                inference_texts, batch_size=self.batch_size
            )

            chunk_embeddings: Dict[str, List[float]] = {}
            for idx, embedding in enumerate(embeddings):
                chunk_embeddings[f"inference_{idx}"] = embedding

            inference_embeddings[chunk_id] = chunk_embeddings

        return inference_embeddings

    def _save_inference_embeddings(
        self, job_id: str, inference_embeddings: Dict[str, Any]
    ) -> None:
        if not inference_embeddings:
            return
        key = f"orchestrator:job:{job_id}:inference_embeddings"
        packed_data = msgpack.packb(inference_embeddings, use_bin_type=True)
        pipe = self.redis_client.pipeline()
        pipe.set(key, packed_data)
        pipe.expire(key, 86400)
        pipe.execute()

    def process_message(self, message: Dict) -> Dict:
        job_id = message.get("job_id")
        chunks = message.get("chunks", [])

        logger.info(f"Processing embeddings for job: {job_id} with {len(chunks)} chunks")

        if not chunks:
            chunks_json = self.redis_client.get(f"orchestrator:job:{job_id}:chunks")
            if chunks_json:
                chunks = json.loads(chunks_json)
            else:
                raise ValueError(f"No chunks found in message or Redis for job: {job_id}")

        embeddings_dict = {}
        total_chunks = len(chunks)

        logger.info(f"Processing {total_chunks} chunks with batch_size={self.batch_size}")

        chunk_texts = [chunk.get("text", "") for chunk in chunks]
        chunk_ids = [chunk.get("chunk_id") for chunk in chunks]

        for batch_start in range(0, len(chunk_texts), self.batch_size):
            batch_end = min(batch_start + self.batch_size, len(chunk_texts))
            batch_texts = chunk_texts[batch_start:batch_end]
            batch_ids = chunk_ids[batch_start:batch_end]

            batch_embeddings = self.service.generate_embeddings(
                batch_texts, batch_size=self.batch_size
            )

            for chunk_id, embedding in zip(batch_ids, batch_embeddings):
                if chunk_id:
                    embeddings_dict[chunk_id] = embedding

            processed = min(batch_end, total_chunks)
            logger.info(f"Generated embeddings for chunk {processed}/{total_chunks}")

        if _detect_gpu():
            self.gpu_memory_gb.set(torch.cuda.memory_allocated() / 1024**3)

        embeddings_key = f"orchestrator:job:{job_id}:embeddings"
        self.redis_client.set(
            embeddings_key, msgpack.packb(embeddings_dict, use_bin_type=True)
        )

        self.redis_client.hset(
            f"orchestrator:job:{job_id}:steps", "embeddings", "completed"
        )

        inference_progress = 33
        if self._check_micro_inferences_exist(job_id):
            logger.info(f"Generating inference embeddings for job: {job_id}")
            try:
                micro_inferences = self._load_micro_inferences(job_id)
                if micro_inferences:
                    inference_embeddings = self._generate_inference_embeddings(micro_inferences)
                    self._save_inference_embeddings(job_id, inference_embeddings)
                    self.redis_client.hset(
                        f"orchestrator:job:{job_id}:steps", "inference_embeddings", "completed"
                    )
                    inference_count = sum(len(c.get("inferences", [])) for c in micro_inferences)
                    logger.info(f"Generated inference embeddings for {inference_count} inferences")
                    inference_progress = 40
            except Exception as e:
                logger.warning(f"Failed to generate inference embeddings: {e}")

        self.event_bus.publish_job_progress(job_id, inference_progress, "embedding")

        logger.info(f"Embeddings completed for job: {job_id} ({total_chunks} chunks)")

        return embeddings_dict


if __name__ == "__main__":
    worker = EmbeddingsWorker()
    worker.run()
