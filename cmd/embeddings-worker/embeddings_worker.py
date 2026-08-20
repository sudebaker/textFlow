#!/usr/bin/env python3
"""
Embeddings Worker for textFlow
Consumes messages from RabbitMQ and generates embeddings for each chunk using BAAI/bge-m3

GPU Features:
- Automatic GPU detection via torch.cuda
- Adaptive batching (GPU=32, CPU=2)
- FP16 optimization for GPU (in EmbeddingService)
"""

import os
import sys

# CRITICAL: Set offline mode BEFORE importing any HuggingFace or transformers packages
os.environ["HF_HUB_OFFLINE"] = "1"
os.environ["TRANSFORMERS_OFFLINE"] = "1"

import json
from typing import Dict, List, Any

import msgpack
import torch

sys.path.insert(0, "/app")
from pkg.worker_common.artifact_store import STORE, resolve_text
from pkg.worker_common.base import BaseWorker
from pkg.worker_common.inference_embeddings import generate_inference_embeddings
from app.services.embeddings import EmbeddingService

QUEUE_NAME = os.getenv("QUEUE_NAME", "embeddings")
METRICS_PORT = int(os.getenv("METRICS_PORT", "8001"))
MODEL_PATH = os.getenv("MODEL_PATH", "/models/bge-m3")
EMBEDDING_BATCH_SIZE_GPU = int(os.getenv("EMBEDDING_BATCH_SIZE_GPU", "32"))
EMBEDDING_BATCH_SIZE_CPU = int(os.getenv("EMBEDDING_BATCH_SIZE_CPU", "2"))

_device_env = os.getenv("EMBEDDINGS_DEVICE", "").strip()
EMBEDDINGS_DEVICE = _device_env if _device_env else None


def detect_gpu() -> bool:
    """Check if GPU is available."""
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

        # Additional GPU memory metric (BaseWorker already has gpu_available)
        from prometheus_client import Gauge
        self.gpu_memory_gb = Gauge(
            "embeddings_worker_gpu_memory_gb", "GPU memory usage in GB"
        )

    def load_model(self):
        use_gpu = detect_gpu()
        self.batch_size = (
            EMBEDDING_BATCH_SIZE_GPU if use_gpu else EMBEDDING_BATCH_SIZE_CPU
        )

        self.gpu_available.labels(device="cuda:0").set(1 if use_gpu else 0)

        if use_gpu:
            gpu_name = (
                torch.cuda.get_device_name() if torch.cuda.is_available() else "Unknown"
            )
            self.logger.info(f"GPU Mode detected: {gpu_name}")
            self.logger.info(f"   Batch size: {self.batch_size} (optimized for GPU)")
        else:
            self.logger.info(f"CPU Mode detected")
            self.logger.info(f"   Batch size: {self.batch_size} (conservative for CPU)")

        self.logger.info(f"Loading embeddings model from: {MODEL_PATH}")
        self.service = EmbeddingService(model_path=MODEL_PATH, device=EMBEDDINGS_DEVICE)
        self.logger.info("Embeddings model loaded successfully")

    def process_message(self, message: Dict) -> Any:
        job_id = message.get("job_id")
        chunks = message.get("chunks", [])

        self.logger.info(
            f"Processing embeddings for job: {job_id} with {len(chunks)} chunks"
        )

        if not chunks:
            chunks_json = resolve_text(
                STORE, self.redis_client.get(f"orchestrator:job:{job_id}:chunks")
            )
            if chunks_json:
                chunks = json.loads(chunks_json)
            else:
                self.logger.warning(
                    f"No chunks found in message or Redis for job: {job_id}"
                )
                self.jobs_total.labels(status="no_chunks").inc()
                return {"status": "no_chunks"}

        embeddings_dict = {}
        total_chunks = len(chunks)

        self.logger.info(
            f"Processing {total_chunks} chunks with batch_size={self.batch_size}"
        )

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
            self.logger.info(
                f"Generated embeddings for chunk {processed}/{total_chunks}"
            )

        if detect_gpu():
            self.gpu_memory_gb.set(torch.cuda.memory_allocated() / 1024**3)

        embeddings_key = f"orchestrator:job:{job_id}:embeddings"
        self.redis_client.set(
            embeddings_key,
            msgpack.packb(embeddings_dict, use_bin_type=True)
        )

        self.redis_client.hset(
            f"orchestrator:job:{job_id}:steps", "embeddings", "completed"
        )

        inference_progress = 33
        micro_inferences_key = f"orchestrator:job:{job_id}:micro_inferences"
        if self.redis_client.exists(micro_inferences_key) > 0:
            self.logger.info(f"Generating inference embeddings for job: {job_id}")
            try:
                raw = self.redis_client.get(micro_inferences_key)
                micro_inferences = json.loads(raw) if raw else []
                if micro_inferences:
                    inferences_by_chunk = {}
                    for chunk_data in micro_inferences:
                        chunk_id = chunk_data.get("chunk_id")
                        infs = chunk_data.get("inferences", [])
                        if chunk_id and infs:
                            inferences_by_chunk[chunk_id] = infs

                    inference_embeddings = generate_inference_embeddings(
                        inferences_by_chunk=inferences_by_chunk,
                        embed_fn=lambda texts: self.service.generate_embeddings(
                            texts, batch_size=self.batch_size
                        ),
                        logger=self.logger,
                    )

                    if inference_embeddings:
                        packed = msgpack.packb(inference_embeddings, use_bin_type=True)
                        ie_key = f"orchestrator:job:{job_id}:inference_embeddings"
                        pipe = self.redis_client.pipeline()
                        pipe.set(ie_key, packed)
                        pipe.expire(ie_key, 86400)
                        pipe.execute()

                        self.redis_client.hset(
                            f"orchestrator:job:{job_id}:steps",
                            "inference_embeddings",
                            "completed",
                        )
                        inference_count = sum(
                            len(c.get("inferences", [])) for c in micro_inferences
                        )
                        self.logger.info(
                            f"Generated inference embeddings for {inference_count} inferences"
                        )
                        inference_progress = 40
            except Exception as e:
                self.logger.warning(f"Failed to generate inference embeddings: {e}")

        self.event_bus.publish_job_progress(job_id, inference_progress, "embedding")

        self.logger.info(
            f"Embeddings completed for job: {job_id} ({total_chunks} chunks)"
        )

        return {"status": "success", "job_id": job_id, "chunks": total_chunks}

    def cleanup(self) -> None:
        super().cleanup()
        if hasattr(self, "service") and self.service is not None:
            del self.service
        if torch.cuda.is_available():
            torch.cuda.empty_cache()


def main():
    worker = EmbeddingsWorker()
    worker.load_model()
    worker.run()


if __name__ == "__main__":
    main()
