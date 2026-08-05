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

import json
import logging
import os
import signal
import sys
from abc import abstractmethod
from typing import Any, Dict, Optional

import redis
from prometheus_client import Counter, Histogram, generate_latest
from fastapi import FastAPI
from fastapi.responses import JSONResponse

sys.path.insert(0, "/app")
from pkg.events_python import EventBus
from pkg.logging_python import setup_logging


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

        self.logger = setup_logging(worker_name)

        self._init_metrics()
        self._init_health_server()
        self._setup_signal_handlers()

    def _init_metrics(self) -> None:
        metrics_prefix = self.worker_name.replace("-", "_")
        self.jobs_total = Counter(
            f"{metrics_prefix}_jobs_total",
            "Total jobs processed",
            ["status"],
        )
        self.job_duration = Histogram(
            f"{metrics_prefix}_job_duration_seconds",
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
            self.logger.info(f"Received signal {signum}, initiating graceful shutdown...")
            self._shutdown_requested = True
            try:
                if self._pubsub:
                    self._pubsub.close()
            except Exception:
                pass
            self.cleanup()

        signal.signal(signal.SIGINT, signal_handler)
        signal.signal(signal.SIGTERM, signal_handler)

    def cleanup(self) -> None:
        """Hook for subclasses to release resources before shutdown."""
        pass

    def _parse_pubsub_message(self, message: Dict) -> Optional[Dict]:
        """Filter control messages and parse JSON data from pub/sub message.

        Returns None if not a data message or if parsing fails.
        """
        if message.get("type") != "message":
            return None
        try:
            return json.loads(message["data"])
        except (json.JSONDecodeError, TypeError, KeyError) as e:
            self.logger.warning(f"Malformed pubsub message: {e}")
            return None

    @property
    def redis_client(self) -> redis.Redis:
        if self._redis_client is None:
            self._redis_client = redis.from_url(
                self.redis_url,
                decode_responses=True,
                socket_keepalive=True,
                socket_connect_timeout=5,
                health_check_interval=0,
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
                health_check_interval=0,
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
        self.logger.info(f"Health server started on port {health_port}")

        from prometheus_client import start_http_server
        metrics_thread = threading.Thread(
            target=start_http_server, args=(self.metrics_port,), daemon=True
        )
        metrics_thread.start()
        self.logger.info(f"Metrics server started on port {self.metrics_port}")

        backoff = 1
        max_backoff = 60

        while not self._shutdown_requested:
            try:
                pubsub = self.redis_client.pubsub()
                self._pubsub = pubsub
                pubsub.subscribe("job:events")
                self.logger.info(f"{self.worker_name} started, listening for job events...")

                backoff = 1

                while not self._shutdown_requested:
                    try:
                        message = pubsub.get_message(timeout=1.0)
                        if message:
                            event = self._parse_pubsub_message(message)
                            if event is not None:
                                try:
                                    self.handle_event(event)
                                except Exception as e:
                                    self.logger.error(f"Error handling event: {e}")
                    except (redis.ConnectionError, redis.TimeoutError) as e:
                        raise e
                    except Exception as e:
                        self.logger.warning(f"Unexpected error in pubsub loop: {e}")
                        raise

            except (redis.ConnectionError, redis.TimeoutError, OSError) as e:
                self.logger.error(f"Error in {self.worker_name} pubsub: {e}")
                try:
                    pubsub.close()
                except Exception:
                    pass

                if not self._shutdown_requested:
                    self.logger.info(f"Reconnecting in {backoff}s...")
                    import time
                    time.sleep(backoff)
                    backoff = min(backoff * 2, max_backoff)

        self.logger.info(f"{self.worker_name} shutdown complete")

    @abstractmethod
    def handle_event(self, message: Dict) -> None:
        raise NotImplementedError("Subclasses must implement handle_event")
