"""
Prometheus metrics utilities for workers.
"""

from typing import Dict

from prometheus_client import Counter, Histogram, Gauge


def create_worker_metrics(worker_name: str) -> Dict:
    """
    Create standard Prometheus metrics for a worker.

    Args:
        worker_name: Name of the worker (e.g., "embeddings", "entities")

    Returns:
        Dict containing metric objects with keys:
            - jobs_total: Counter for total jobs processed
            - job_duration: Histogram for job duration
            - errors_total: Counter for total errors
            - queue_depth: Gauge for queue depth (if applicable)

    Example:
        >>> metrics = create_worker_metrics("embeddings")
        >>> metrics["jobs_total"].labels(status="success").inc()
        >>> with metrics["job_duration"].time():
        >>>     process_job()
    """
    metrics = {
        "jobs_total": Counter(
            f"{worker_name}_worker_jobs_total",
            f"Total jobs processed by {worker_name} worker",
            ["status"],
        ),
        "job_duration": Histogram(
            f"{worker_name}_worker_job_duration_seconds",
            f"Job duration for {worker_name} worker in seconds",
            buckets=[0.1, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0, 120.0],
        ),
        "errors_total": Counter(
            f"{worker_name}_worker_errors_total",
            f"Total errors in {worker_name} worker",
            ["error_type"],
        ),
    }

    return metrics


def create_gpu_metrics(worker_name: str) -> Gauge:
    """
    Create GPU availability metric.

    Args:
        worker_name: Name of the worker

    Returns:
        Gauge for GPU availability
    """
    return Gauge(
        f"{worker_name}_worker_gpu_available",
        f"GPU availability for {worker_name} worker",
        ["device"],
    )
