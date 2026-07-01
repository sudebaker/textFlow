"""
BasePubSubWorker class for workers that use Redis pub/sub instead of RabbitMQ.

Provides common functionality for completion-worker:
- Redis pub/sub connection with exponential backoff reconnection
- Signal handling for graceful shutdown
- Prometheus metrics server
- FastAPI health check endpoints
- Event Bus integration

Usage:
    from pkg.worker_common.pubsub_base import BasePubSubWorker

    class CompletionWorker(BasePubSubWorker):
        def handle_event(self, message: Dict) -> None:
            # implement event handling logic
            pass

    if __name__ == "__main__":
        worker = CompletionWorker("completion-worker", metrics_port=8005)
        worker.start()
"""

import logging
import os
import signal
import sys
import threading
from abc import abstractmethod
from typing import Any, Dict, Optional

import redis
from prometheus_client import Counter, Histogram, generate_latest
from fastapi import FastAPI, JSONResponse

sys.path.insert(0, "/app")
from pkg.events_python import EventBus

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger("worker_common.pubsub_base")


class BasePubSubWorker:
    def __init__(
        self,
        worker_name: str,
        metrics_port: int,
    ):
        self.worker_name = worker_name
        self.metrics_port = metrics_port
        self._shutdown_requested = False

        self.redis_url = os.getenv("REDIS_URL", "redis://localhost:6379")

        self._redis_client: Optional[redis.Redis] = None
        self._redis_raw: Optional[redis.Redis] = None
        self._event_bus: Optional[EventBus] = None
        self._pubsub: Optional[redis.client.PubSub] = None

        self._init_metrics()
        self._init_health_server()
        self._setup_signal_handlers()

    def _init_metrics(self) -> None:
        self.jobs_total = Counter(
            f"{self.worker_name}_jobs_total",
            "Total jobs processed",
            ["status"],
        )
        self.job_duration = Histogram(
            f"{self.worker_name}_job_duration_seconds",
            "Job duration in seconds",
            buckets=[0.1, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0],
        )

    def _init_health_server(self) -> None:
        self.app = FastAPI(title=f"{self.worker_name}-health")

        @self.app.get("/health")
        async def health():
            status = "healthy"
            checks = {}
            try:
                self.redis_client.ping()
                checks["redis"] = "ok"
            except Exception as e:
                checks["redis"] = f"error: {e}"
                status = "degraded"

            return JSONResponse({
                "status": status,
                "worker": self.worker_name,
                "checks": checks,
            })

        @self.app.get("/metrics")
        async def metrics():
            return JSONResponse(
                generate_latest().decode("utf-8"),
                media_type="text/plain; version=0.0.4; charset=utf-8",
            )

        @self.app.get("/ready")
        async def ready():
            if self._shutdown_requested:
                return JSONResponse(
                    {"ready": False, "reason": "shutdown_pending"}, status_code=503
                )
            return JSONResponse({"ready": True})

    def _setup_signal_handlers(self) -> None:
        def signal_handler(signum, frame):
            logger.info(f"Received signal {signum}, initiating graceful shutdown...")
            self._shutdown_requested = True
            try:
                if self._pubsub:
                    self._pubsub.close()
            except Exception:
                pass

        signal.signal(signal.SIGINT, signal_handler)
        signal.signal(signal.SIGTERM, signal_handler)

    @property
    def redis_client(self) -> redis.Redis:
        if self._redis_client is None:
            self._redis_client = redis.from_url(
                self.redis_url,
                decode_responses=True,
                socket_keepalive=True,
                socket_connect_timeout=5,
                health_check_interval=30,
            )
        return self._redis_client

    @property
    def redis_raw(self) -> redis.Redis:
        if self._redis_raw is None:
            self._redis_raw = redis.from_url(
                self.redis_url,
                decode_responses=False,
                socket_keepalive=True,
                socket_connect_timeout=5,
                health_check_interval=30,
            )
        return self._redis_raw

    @property
    def event_bus(self) -> EventBus:
        if self._event_bus is None:
            self._event_bus = EventBus(self.redis_client)
        return self._event_bus

    def start(self) -> None:
        import threading

        health_port = self.metrics_port + 1000

        def start_uvicorn():
            import uvicorn
            uvicorn.run(self.app, host="0.0.0.0", port=health_port, log_level="warning")

        health_thread = threading.Thread(target=start_uvicorn, daemon=True)
        health_thread.start()
        logger.info(f"Health server started on port {health_port}")

        from prometheus_client import start_http_server
        metrics_thread = threading.Thread(
            target=start_http_server, args=(self.metrics_port,), daemon=True
        )
        metrics_thread.start()
        logger.info(f"Metrics server started on port {self.metrics_port}")

        backoff = 1
        max_backoff = 60

        while not self._shutdown_requested:
            try:
                pubsub = self.redis_client.pubsub()
                self._pubsub = pubsub
                pubsub.subscribe("job:events")
                logger.info(f"{self.worker_name} started, listening for job events...")

                backoff = 1

                for message in pubsub.listen():
                    if self._shutdown_requested:
                        break
                    try:
                        self.handle_event(message)
                    except Exception as e:
                        logger.error(f"Error handling event: {e}")

            except Exception as e:
                logger.error(f"Error in {self.worker_name} pubsub: {e}")
                try:
                    pubsub.close()
                except Exception:
                    pass

                if not self._shutdown_requested:
                    logger.info(f"Reconnecting in {backoff}s...")
                    import time
                    time.sleep(backoff)
                    backoff = min(backoff * 2, max_backoff)

        logger.info(f"{self.worker_name} shutdown complete")

    @abstractmethod
    def handle_event(self, message: Dict) -> None:
        raise NotImplementedError("Subclasses must implement handle_event")
