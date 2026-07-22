"""
Resource Manager client for workers.
"""

import logging
import time
from typing import Dict, Optional

import requests

logger = logging.getLogger(__name__)


class ResourceManagerClient:
    """
    Client for communicating with the Resource Manager service.

    Provides resource allocation and health check functionality with caching.
    """

    def __init__(self, base_url: str, cache_ttl: int = 60):
        """
        Initialize ResourceManagerClient.

        Args:
            base_url: Base URL of Resource Manager (e.g., http://localhost:9090)
            cache_ttl: Cache time-to-live in seconds (default: 60)
        """
        self.base_url = base_url.rstrip("/")
        self.cache_ttl = cache_ttl
        self._cache: Optional[Dict] = None
        self._cache_time: float = 0

    def allocate_resource(self, resource_type: str, worker_id: str) -> Dict:
        """
        Allocate a resource for the worker.

        Args:
            resource_type: Type of resource (e.g., "gpu", "cpu")
            worker_id: Unique identifier for the worker

        Returns:
            Dict containing allocation details

        Raises:
            requests.RequestException: If allocation fails
        """
        try:
            response = requests.post(
                f"{self.base_url}/resources/allocate",
                json={"resource_type": resource_type, "worker_id": worker_id},
                timeout=5,
            )
            response.raise_for_status()
            return response.json()
        except requests.RequestException as e:
            logger.error(f"Failed to allocate resource: {e}")
            # Return empty dict to allow graceful degradation
            return {}

    def release_resource(self, resource_id: str) -> bool:
        """
        Release a previously allocated resource.

        Args:
            resource_id: ID of the resource to release

        Returns:
            True if release was successful, False otherwise
        """
        try:
            response = requests.post(
                f"{self.base_url}/resources/release",
                json={"resource_id": resource_id},
                timeout=5,
            )
            response.raise_for_status()
            return True
        except requests.RequestException as e:
            logger.error(f"Failed to release resource: {e}")
            return False

    def health_check(self) -> bool:
        """
        Check if Resource Manager is healthy.

        Uses caching to avoid overwhelming the service.

        Returns:
            True if healthy, False otherwise
        """
        # Check cache first
        now = time.time()
        if self._cache is not None and (now - self._cache_time) < self.cache_ttl:
            return self._cache.get("status") == "healthy"

        # Make request
        try:
            response = requests.get(f"{self.base_url}/health", timeout=2)
            response.raise_for_status()
            data = response.json()

            # Update cache
            self._cache = data
            self._cache_time = now

            return data.get("status") == "healthy"
        except requests.RequestException as e:
            logger.warning(f"Resource Manager health check failed: {e}")
            return False

    def get_available_resources(self) -> Dict:
        """
        Get list of available resources.

        Returns:
            Dict containing available resources by type
        """
        try:
            response = requests.get(f"{self.base_url}/resources", timeout=5)
            response.raise_for_status()
            return response.json()
        except requests.RequestException as e:
            logger.error(f"Failed to get available resources: {e}")
            return {}
