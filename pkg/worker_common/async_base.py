"""
BaseAsyncWorker class for async Python workers (extraction, audio, image).

Provides common functionality for workers that use aio_pika:
- RabbitMQ connection with retry and DLX queue declaration
- Redis connection
- Signal handling for graceful shutdown (SIGINT/SIGTERM)
- Prometheus metrics server
- FastAPI health check endpoints
- Event Bus integration
- Helper for running blocking service calls in executor

Usage:
    from pkg.worker_common.async_base import BaseAsyncWorker

    class MyWorker(BaseAsyncWorker):
        async def process_message(self, message: Dict) -> None:
            # implement worker-specific logic
            pass

    if __name__ == "__main__":
        worker = MyWorker("my-worker", "my_queue", metrics_port=8004)
        asyncio.run(worker.run())
"""

import asyncio
import json
import logging
import os
import signal
import sys
import threading
from abc import abstractmethod
from typing import Any, Dict, List, Optional

import redis
from prometheus_client import Counter, Histogram, Gauge, generate_latest
from fastapi import FastAPI, JSONResponse

sys.path.insert(0, "/app")
from pkg.events_python import EventBus

DLX_EXCHANGE = "document_processor_dlx"

MAX_RETRIES = int(os.environ.get("MAX_RETRIES", "3"))

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger("worker_common.async_base")


class BaseAsyncWorker:
    def __init__(
        self,
        worker_name: str,
        queue_name: str,
        metrics_port: int,
        requires_gpu: bool = False,
    ):
        self.worker_name = worker_name
        self.queue_name = queue_name
        self.requires_gpu = requires_gpu
        self.metrics_port = metrics_port
        self.max_retries = MAX_RETRIES
        self._shutdown_requested = False
        self._stopping = False
        self._rabbitmq_connected = False

        self.redis_url = os.getenv("REDIS_URL", "redis://localhost:6379")
        self.rabbitmq_url = os.getenv("RABBITMQ_URL", "amqp://localhost:5672/")
        self.prefetch_count = int(os.getenv("PREFETCH_COUNT", "10"))

        self._redis_client: Optional[redis.Redis] = None
        self._event_bus: Optional[EventBus] = None
        self._channel: Optional[Any] = None

        self._init_metrics()
        self._init_health_server()

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
        self.gpu_available = Gauge(
            f"{self.worker_name}_gpu_available", "GPU availability", ["device"]
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

            if self._rabbitmq_connected:
                checks["rabbitmq"] = "ok"
            else:
                checks["rabbitmq"] = "connecting"
                status = "degraded"

            return JSONResponse({"status": status, "worker": self.worker_name, "checks": checks})

        @self.app.get("/metrics")
        async def metrics():
            return JSONResponse(
                generate_latest().decode("utf-8"),
                media_type="text/plain; version=0.0.4; charset=utf-8",
            )

        @self.app.get("/ready")
        async def ready():
            if self._shutdown_requested:
                return JSONResponse({"ready": False, "reason": "shutdown_pending"}, status_code=503)
            return JSONResponse({"ready": True})

    @property
    def redis_client(self) -> redis.Redis:
        if self._redis_client is None:
            self._redis_client = redis.from_url(self.redis_url, decode_responses=True)
        return self._redis_client

    @property
    def event_bus(self) -> EventBus:
        if self._event_bus is None:
            self._event_bus = EventBus(self.redis_client)
        return self._event_bus

    async def connect_rabbitmq(self) -> tuple:
        import aio_pika

        last_exc: Optional[Exception] = None
        for attempt in range(1, self.max_retries + 1):
            try:
                connection = await aio_pika.connect_robust(self.rabbitmq_url)
                channel = await connection.channel()
                await channel.set_qos(prefetch_count=self.prefetch_count)
                queue = await channel.declare_queue(
                    self.queue_name,
                    durable=True,
                    arguments={
                        "x-dead-letter-exchange": DLX_EXCHANGE,
                        "x-dead-letter-routing-key": f"{self.queue_name}_failed",
                    },
                )
                logger.info(
                    f"Connected to RabbitMQ at {self.rabbitmq_url}, "
                    f"queue={self.queue_name}, prefetch={self.prefetch_count}"
                )
                return connection, channel, queue
            except Exception as exc:
                last_exc = exc
                wait = 2 ** attempt
                logger.warning(
                    f"RabbitMQ connect attempt {attempt}/{self.max_retries} failed: {exc}. "
                    f"Retrying in {wait}s"
                )
                await asyncio.sleep(wait)
        raise RuntimeError(
            f"Failed to connect to RabbitMQ after {self.max_retries} retries: {last_exc}"
        )

    async def publish_downstream(
        self,
        channel: Any,
        queues: List[str],
        message: Dict,
    ) -> None:
        import aio_pika
        for queue in queues:
            await channel.default_exchange.publish(
                aio_pika.Message(
                    body=json.dumps(message).encode(),
                    delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
                ),
                routing_key=queue,
            )

    def run_in_executor(self, func, *args) -> asyncio.Future:
        loop = asyncio.get_running_loop()
        return loop.run_in_executor(None, lambda: func(*args))

    async def run(self) -> None:
        import aio_pika

        logger.info(f"Starting {self.worker_name}")
        logger.info(f"Queue: {self.queue_name}")

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

        stop_event = asyncio.Event()

        def signal_handler(signum, frame):
            logger.info(f"Received signal {signum}, initiating graceful shutdown...")
            self._shutdown_requested = True
            self._stopping = True
            stop_event.set()

        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, signal_handler, sig, None)
            except NotImplementedError:
                pass

        while not self._shutdown_requested:
            connection = None
            channel = None
            try:
                connection, channel, queue = await self.connect_rabbitmq()
                self._rabbitmq_connected = True
                self._channel = channel

                async def on_message(message: aio_pika.IncomingMessage):
                    async with message.process(requeue=False):
                        if self._shutdown_requested:
                            return
                        start_time = asyncio.get_event_loop().time()
                        job_id = None
                        try:
                            body = json.loads(message.body.decode())
                            job_id = body.get("job_id")
                            await self.process_message(body)
                            duration = asyncio.get_event_loop().time() - start_time
                            self.job_duration.observe(duration)
                            self.jobs_total.labels(status="success").inc()
                            logger.info(f"Job {job_id} completed in {duration:.2f}s")
                        except (ConnectionError, TimeoutError) as e:
                            duration = asyncio.get_event_loop().time() - start_time
                            self.jobs_total.labels(status="transient_error").inc()
                            logger.warning(f"Job {job_id} transient error: {e}")
                            raise
                        except Exception as e:
                            duration = asyncio.get_event_loop().time() - start_time
                            self.jobs_total.labels(status="error").inc()
                            logger.error(f"Job {job_id} failed permanently: {e}")
                            if job_id:
                                try:
                                    self.redis_client.set(
                                        f"orchestrator:job:{job_id}:status", "failed"
                                    )
                                    self.redis_client.set(
                                        f"orchestrator:job:{job_id}:error", str(e)
                                    )
                                except Exception:
                                    pass

                logger.info(f"Consuming from queue: {self.queue_name}")
                await queue.consume(on_message)

                while not stop_event.is_set():
                    try:
                        await asyncio.wait_for(stop_event.wait(), timeout=1.0)
                    except asyncio.TimeoutError:
                        pass

                if channel and channel.is_open:
                    await channel.close()
                if connection and not connection.is_closed:
                    await connection.close()

            except Exception as e:
                self._rabbitmq_connected = False
                logger.error(f"RabbitMQ error: {e}")
                if not self._shutdown_requested:
                    await asyncio.sleep(5)

        logger.info(f"{self.worker_name} shutdown complete")

    @abstractmethod
    async def process_message(self, message: Dict) -> None:
        raise NotImplementedError("Subclasses must implement process_message")
