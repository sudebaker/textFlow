"""
Embedding generation service using sentence-transformers.

This module provides embedding generation capabilities using the
BAAI/bge-m3 multilingual model with GPU/CPU support.
"""

import os
import time
import logging
from typing import List, Dict, Any, Optional
import threading

logger = logging.getLogger(__name__)

# Try to import sentence-transformers, handle gracefully if not available
try:
    from sentence_transformers import SentenceTransformer
    import torch

    SENTENCE_TRANSFORMERS_AVAILABLE = True
except ImportError as e:
    logger.error(f"sentence-transformers not available: {e}")
    SENTENCE_TRANSFORMERS_AVAILABLE = False
    SentenceTransformer = None
    torch = None


class EmbeddingService:
    """
    Service for generating text embeddings using BAAI/bge-m3.

    This service handles model loading, embedding generation,
    and provides thread-safe operations for concurrent requests.
    """

    def __init__(
        self,
        model_name: str = "BAAI/bge-m3",
        model_path: str = None,
        device: str = "cpu",
    ):
        """
        Initialize the embedding service.

        Args:
            model_name: Name of the sentence-transformers model
            model_path: Local path to the model (optional)
            device: Device to use ('cpu' or 'cuda')
        """
        if not SENTENCE_TRANSFORMERS_AVAILABLE:
            raise ImportError(
                "sentence-transformers is required. "
                "Install with: pip install sentence-transformers torch"
            )

        self.model_name = model_name
        self.model_path = model_path
        self.device = device
        self.model = None
        self.embedding_dimension = 1024  # BAAI/bge-m3 dimension
        self._model_lock = threading.Lock()

        # Set CUDA environment variables if GPU is requested
        if device == "cuda" and torch.cuda.is_available():
            logger.info(f"Using GPU device: {torch.cuda.get_device_name()}")
        elif device == "cuda":
            logger.warning("CUDA requested but not available, falling back to CPU")
            self.device = "cpu"
        else:
            logger.info("Using CPU device")

        # Initialize model on first use
        self._model_loaded = False

    def load_model(self) -> bool:
        """
        Load the embedding model.

        Returns:
            bool: True if model loaded successfully, False otherwise
        """
        if not SENTENCE_TRANSFORMERS_AVAILABLE:
            logger.error("sentence-transformers not available")
            return False

        with self._model_lock:
            if self._model_loaded:
                return True

            try:
                logger.info(f"Loading embedding model...")

                # Check if local model path exists
                model_path = self.model_path if self.model_path else self.model_name

                from pathlib import Path

                local_path = Path(model_path)

                # Check if sentence_transformers.cfg or config.json exists
                has_local_model = (
                    local_path / "sentence_transformers.cfg"
                ).exists() or (local_path / "config.json").exists()

                if has_local_model and local_path.exists():
                    logger.info(f"Loading model from local path: {model_path}")
                    self.model = SentenceTransformer(
                        str(local_path), device=self.device
                    )
                else:
                    logger.info(
                        f"Local model not found at {model_path}, downloading from HuggingFace..."
                    )
                    self.model = SentenceTransformer(
                        self.model_name, device=self.device
                    )

                # Optimize for inference
                if hasattr(self.model, "eval"):
                    self.model.eval()

                self._model_loaded = True
                logger.info(f"Model loaded successfully on {self.device}")
                return True

            except Exception as e:
                logger.error(f"Failed to load model: {e}")
                return False

    def generate_embeddings(
        self, texts: List[str], batch_size: int = 32, show_progress: bool = False
    ) -> List[List[float]]:
        """
        Generate embeddings for a list of texts.

        Args:
            texts: List of texts to embed
            batch_size: Batch size for processing
            show_progress: Whether to show progress bar

        Returns:
            List of embedding vectors

        Raises:
            RuntimeError: If model is not loaded or generation fails
        """
        if not self._model_loaded:
            if not self.load_model():
                raise RuntimeError("Failed to load embedding model")

        if not texts:
            return []

        try:
            start_time = time.time()

            # Generate embeddings with batching
            embeddings = self.model.encode(
                texts,
                batch_size=batch_size,
                show_progress_bar=show_progress,
                convert_to_numpy=True,
                normalize_embeddings=True,  # Important for similarity search
            )

            # Convert to list of floats
            embedding_list = embeddings.tolist()

            processing_time = time.time() - start_time
            logger.debug(
                f"Generated {len(embedding_list)} embeddings "
                f"in {processing_time:.2f}s "
                f"(avg: {processing_time / len(embedding_list):.3f}s per embedding)"
            )

            return embedding_list

        except Exception as e:
            logger.error(f"Failed to generate embeddings: {e}")
            raise RuntimeError(f"Embedding generation failed: {e}")

    def generate_single_embedding(self, text: str) -> List[float]:
        """
        Generate embedding for a single text.

        Args:
            text: Text to embed

        Returns:
            Embedding vector as list of floats
        """
        return self.generate_embeddings([text])[0]

    def is_model_loaded(self) -> bool:
        """Check if the model is loaded."""
        return self._model_loaded and self.model is not None

    def get_model_info(self) -> Dict[str, Any]:
        """
        Get information about the loaded model.

        Returns:
            Dictionary with model information
        """
        info = {
            "model_name": self.model_name,
            "model_path": self.model_path,
            "device": self.device,
            "embedding_dimension": self.embedding_dimension,
            "model_loaded": self.is_model_loaded(),
            "sentence_transformers_available": SENTENCE_TRANSFORMERS_AVAILABLE,
        }

        # Add GPU information if available
        if torch and torch.cuda.is_available():
            info.update(
                {
                    "cuda_available": True,
                    "cuda_device_count": torch.cuda.device_count(),
                    "cuda_current_device": torch.cuda.current_device(),
                    "cuda_device_name": torch.cuda.get_device_name(),
                    "cuda_memory_allocated": torch.cuda.memory_allocated()
                    / 1024**3,  # GB
                    "cuda_memory_reserved": torch.cuda.memory_reserved()
                    / 1024**3,  # GB
                }
            )
        else:
            info["cuda_available"] = False

        return info

    def health_check(self) -> Dict[str, str]:
        """
        Perform health check on the embedding service.

        Returns:
            Dictionary with health check results
        """
        checks = {}

        # Check sentence-transformers availability
        if not SENTENCE_TRANSFORMERS_AVAILABLE:
            checks["sentence_transformers"] = (
                "unavailable: sentence-transformers not installed"
            )
            return checks
        else:
            checks["sentence_transformers"] = "ok"

        # Check model loading
        if not self.is_model_loaded():
            try:
                if self.load_model():
                    checks["model"] = "ok"
                else:
                    checks["model"] = "failed: could not load model"
            except Exception as e:
                checks["model"] = f"failed: {e}"
        else:
            checks["model"] = "ok"

        # Test embedding generation with a simple text
        try:
            test_embedding = self.generate_single_embedding("test")
            if len(test_embedding) == self.embedding_dimension:
                checks["embedding_generation"] = "ok"
            else:
                checks["embedding_generation"] = (
                    f"failed: wrong dimension {len(test_embedding)}"
                )
        except Exception as e:
            checks["embedding_generation"] = f"failed: {e}"

        # Check device status
        if self.device == "cuda" and torch:
            if torch.cuda.is_available():
                checks["cuda"] = "ok"
            else:
                checks["cuda"] = "unavailable: CUDA not available"
        else:
            checks["cuda"] = "not_used"

        return checks

    def get_memory_usage(self) -> Dict[str, float]:
        """
        Get memory usage information.

        Returns:
            Dictionary with memory usage stats
        """
        memory_info = {}

        if torch and torch.cuda.is_available():
            memory_info.update(
                {
                    "cuda_allocated_gb": torch.cuda.memory_allocated() / 1024**3,
                    "cuda_reserved_gb": torch.cuda.memory_reserved() / 1024**3,
                    "cuda_max_allocated_gb": torch.cuda.max_memory_allocated()
                    / 1024**3,
                }
            )

        return memory_info

    def cleanup(self):
        """Cleanup resources and free memory."""
        if self.model:
            del self.model
            self.model = None
            self._model_loaded = False

        if torch and torch.cuda.is_available():
            torch.cuda.empty_cache()

        logger.info("Embedding service cleaned up")
