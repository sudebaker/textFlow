"""
Example refactored worker using worker_common library.

This is a simplified example showing how to structure a worker using the
shared utilities from the worker_common package.

Compare this with the existing workers in:
- cmd/embeddings-worker/embeddings_worker.py
- cmd/entities-worker/entities_worker.py
- cmd/metadata-worker/worker.py

To see the benefits of using worker_common.
"""

import json
import logging
import time
from typing import Dict, Any

from worker_common.config import WorkerConfig, get_env, get_int_env
from worker_common.rabbitmq import rabbitmq_connection
from worker_common.resource_manager import ResourceManagerClient
from worker_common.metrics import create_worker_metrics, create_gpu_metrics
from worker_common.signals import SignalHandler

# Configure logging
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)


class ExampleWorker:
    """
    Example worker demonstrating worker_common usage.

    This worker processes messages from RabbitMQ, performs some work,
    and reports metrics.
    """

    def __init__(self, config: WorkerConfig):
        """Initialize the worker with configuration."""
        self.config = config
        self.signal_handler = SignalHandler()
        self.resource_client = ResourceManagerClient(config.resource_manager_url)

        # Create metrics
        self.metrics = create_worker_metrics(config.worker_name)
        self.gpu_metrics = create_gpu_metrics(config.worker_name)

        logger.info(f"Initialized {config.worker_name}")

    def process_message(self, body: bytes) -> Dict[str, Any]:
        """
        Process a single message.

        Args:
            body: Raw message body from RabbitMQ

        Returns:
            Processing result dictionary
        """
        start_time = time.time()

        try:
            # Parse message
            message = json.loads(body)
            job_id = message.get("job_id")
            data = message.get("data")

            logger.info(f"Processing job {job_id}")

            # Acquire GPU resource (if needed)
            resource = None
            if self.config.requires_gpu:
                resource = self.resource_client.acquire_resource(
                    resource_type="gpu", worker_id=self.config.worker_name
                )
                logger.info(f"Acquired resource: {resource['resource_id']}")

            try:
                # TODO: Replace with actual processing logic
                result = self._do_work(data)

                # Update success metrics
                self.metrics["messages_processed"].labels(
                    worker=self.config.worker_name, status="success"
                ).inc()

                return {"job_id": job_id, "status": "success", "result": result}

            finally:
                # Release resource
                if resource:
                    self.resource_client.release_resource(resource["resource_id"])
                    logger.info(f"Released resource: {resource['resource_id']}")

        except json.JSONDecodeError as e:
            logger.error(f"Invalid JSON: {e}")
            self.metrics["messages_processed"].labels(
                worker=self.config.worker_name, status="error"
            ).inc()
            return {"status": "error", "error": "Invalid JSON"}

        except Exception as e:
            logger.error(f"Processing failed: {e}", exc_info=True)
            self.metrics["messages_processed"].labels(
                worker=self.config.worker_name, status="error"
            ).inc()
            return {"status": "error", "error": str(e)}

        finally:
            # Record processing time
            duration = time.time() - start_time
            self.metrics["processing_time"].labels(
                worker=self.config.worker_name
            ).observe(duration)
            logger.info(f"Processing took {duration:.3f}s")

    def _do_work(self, data: Any) -> Any:
        """
        Perform the actual work.

        Replace this with your worker's specific logic:
        - Embeddings generation
        - Entity extraction
        - Metadata processing
        - etc.
        """
        # Simulate work
        time.sleep(0.1)
        return {"processed": True, "data": data}

    def run(self):
        """
        Main worker loop.

        Connects to RabbitMQ and processes messages until shutdown signal.
        """
        logger.info(f"Starting {self.config.worker_name}")
        logger.info(f"Queue: {self.config.queue_name}")
        logger.info(f"Prefetch: {self.config.prefetch_count}")

        # Start metrics server
        self.config.start_metrics_server()

        # Connect to RabbitMQ
        with rabbitmq_connection(self.config.rabbitmq_url) as (connection, channel):
            # Configure QoS
            channel.basic_qos(prefetch_count=self.config.prefetch_count)

            # Declare queue
            channel.queue_declare(queue=self.config.queue_name, durable=True)

            logger.info(f"Waiting for messages on {self.config.queue_name}")

            # Process messages
            for method_frame, properties, body in channel.consume(
                queue=self.config.queue_name, auto_ack=False
            ):
                # Check for shutdown signal
                if self.signal_handler.should_shutdown:
                    logger.info("Shutdown signal received, stopping consumption")
                    channel.cancel()
                    break

                # Process message
                try:
                    result = self.process_message(body)

                    # TODO: Store result in Redis or send to another queue
                    logger.info(f"Result: {result}")

                    # Acknowledge message
                    channel.basic_ack(delivery_tag=method_frame.delivery_tag)

                except Exception as e:
                    logger.error(f"Failed to process message: {e}", exc_info=True)

                    # Reject and requeue message
                    channel.basic_nack(
                        delivery_tag=method_frame.delivery_tag, requeue=True
                    )

        logger.info(f"{self.config.worker_name} shut down gracefully")


def main():
    """Main entry point."""
    # Load configuration from environment
    config = WorkerConfig(
        rabbitmq_url=get_env("RABBITMQ_URL", "amqp://guest:guest@localhost:5672/"),
        redis_host=get_env("REDIS_HOST", "localhost"),
        redis_port=get_int_env("REDIS_PORT", 6379),
        queue_name=get_env("QUEUE_NAME", "example_queue"),
        prefetch_count=get_int_env("PREFETCH_COUNT", 5),
        resource_manager_url=get_env("RESOURCE_MANAGER_URL", "http://localhost:8081"),
        worker_name=get_env("WORKER_NAME", "example-worker"),
        metrics_port=get_int_env("METRICS_PORT", 8001),
        requires_gpu=get_env("REQUIRES_GPU", "false").lower() == "true",
    )

    # Create and run worker
    worker = ExampleWorker(config)
    worker.run()


if __name__ == "__main__":
    main()
