"""
Text chunking service for embeddings processing.

This module provides deterministic text chunking with overlap support
for embedding generation services.
"""

from typing import List, Dict, Any, Optional
import logging


logger = logging.getLogger(__name__)


class ChunkingService:
    """
    Service for deterministic text chunking with overlap.
    
    This service splits text into chunks with configurable size and overlap,
    ensuring consistent results for the same input parameters.
    """
    
    def __init__(self):
        """Initialize the chunking service."""
        logger.debug("Chunking service initialized")
    
    def chunk_text(
        self,
        text: str,
        chunk_size: int = 512,
        chunk_overlap: int = 64,
        doc_id: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None
    ) -> List[Dict[str, Any]]:
        """
        Split text into chunks with overlap.
        
        Args:
            text: The input text to chunk
            chunk_size: Size of each chunk in characters
            chunk_overlap: Overlap between consecutive chunks
            doc_id: Optional document identifier
            metadata: Optional metadata to inherit by all chunks
            
        Returns:
            List of dictionaries containing chunk data and metadata
            
        Raises:
            ValueError: If parameters are invalid
        """
        # Validate parameters
        if chunk_size <= 0:
            raise ValueError("chunk_size must be greater than 0")
        
        if chunk_overlap < 0:
            raise ValueError("chunk_overlap must be non-negative")
        
        if chunk_overlap >= chunk_size:
            raise ValueError("chunk_overlap must be less than chunk_size")
        
        if not text or not text.strip():
            raise ValueError("text cannot be empty")
        
        # Clean and normalize text
        text = text.strip()
        
        # If text is shorter than chunk_size, return single chunk
        if len(text) <= chunk_size:
            return [self._create_chunk(
                text=text,
                chunk_index=0,
                doc_id=doc_id,
                metadata=metadata,
                chunk_size=len(text),
                overlap=0
            )]
        
        chunks = []
        step_size = chunk_size - chunk_overlap
        
        # Create chunks with overlap
        for i in range(0, len(text), step_size):
            # Calculate chunk boundaries
            start = i
            end = min(i + chunk_size, len(text))
            
            # Get the chunk text
            chunk_text = text[start:end]
            
            # Skip empty chunks (can happen with overlap at the end)
            if not chunk_text.strip():
                continue
            
            # Calculate actual overlap for this chunk
            actual_overlap = chunk_overlap if i > 0 else 0
            
            # Create chunk with metadata
            chunk = self._create_chunk(
                text=chunk_text,
                chunk_index=len(chunks),
                doc_id=doc_id,
                metadata=metadata,
                chunk_size=len(chunk_text),
                overlap=actual_overlap,
                start_char=start,
                end_char=end
            )
            
            chunks.append(chunk)
            
            # Break if this chunk reaches the end of text
            if end >= len(text):
                break
        
        logger.debug(
            f"Text chunked into {len(chunks)} pieces "
            f"(size={chunk_size}, overlap={chunk_overlap}, text_length={len(text)})"
        )
        
        return chunks
    
    def _create_chunk(
        self,
        text: str,
        chunk_index: int,
        doc_id: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        chunk_size: Optional[int] = None,
        overlap: int = 0,
        start_char: Optional[int] = None,
        end_char: Optional[int] = None
    ) -> Dict[str, Any]:
        """
        Create a chunk dictionary with metadata.
        
        Args:
            text: The chunk text
            chunk_index: Index of this chunk
            doc_id: Optional document identifier
            metadata: Optional metadata to inherit
            chunk_size: Size of this chunk
            overlap: Overlap with previous chunk
            start_char: Start character position in original text
            end_char: End character position in original text
            
        Returns:
            Dictionary containing chunk data and metadata
        """
        # Start with basic metadata
        chunk_metadata = {
            "chunk_index": chunk_index,
            "chunk_size": chunk_size or len(text),
            "overlap": overlap,
        }
        
        # Add position information if available
        if start_char is not None:
            chunk_metadata["start_char"] = start_char
        if end_char is not None:
            chunk_metadata["end_char"] = end_char
        
        # Add document identifier if provided
        if doc_id:
            chunk_metadata["doc_id"] = doc_id
        
        # Inherit additional metadata if provided
        if metadata:
            chunk_metadata.update(metadata)
        
        return {
            "text": text,
            "metadata": chunk_metadata
        }
    
    def validate_chunking_parameters(
        self,
        text: str,
        chunk_size: int,
        chunk_overlap: int
    ) -> None:
        """
        Validate chunking parameters.
        
        Args:
            text: The text to be chunked
            chunk_size: Desired chunk size
            chunk_overlap: Desired overlap
            
        Raises:
            ValueError: If any parameter is invalid
        """
        if chunk_size <= 0:
            raise ValueError("chunk_size must be greater than 0")
        
        if chunk_overlap < 0:
            raise ValueError("chunk_overlap must be non-negative")
        
        if chunk_overlap >= chunk_size:
            raise ValueError("chunk_overlap must be less than chunk_size")
        
        if not text or not text.strip():
            raise ValueError("text cannot be empty")
        
        if len(text) > 1048576:  # 1MB limit
            raise ValueError("text size exceeds maximum allowed size (1MB)")
    
    def get_chunk_statistics(
        self,
        text: str,
        chunk_size: int,
        chunk_overlap: int
    ) -> Dict[str, int]:
        """
        Get statistics about chunking without performing it.
        
        Args:
            text: The text to analyze
            chunk_size: Desired chunk size
            chunk_overlap: Desired overlap
            
        Returns:
            Dictionary with chunking statistics
        """
        if not text or chunk_size <= 0:
            return {"total_chunks": 0, "total_characters": 0}
        
        text_length = len(text)
        
        if text_length <= chunk_size:
            return {
                "total_chunks": 1,
                "total_characters": text_length,
                "avg_chunk_size": text_length
            }
        
        step_size = chunk_size - chunk_overlap
        total_chunks = (text_length - chunk_overlap) // step_size
        if (text_length - chunk_overlap) % step_size != 0:
            total_chunks += 1
        
        return {
            "total_chunks": total_chunks,
            "total_characters": text_length,
            "avg_chunk_size": min(chunk_size, text_length),
            "step_size": step_size
        }