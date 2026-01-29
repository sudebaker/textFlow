"""
Tests for chunking service functionality.
"""

import pytest
from app.services.chunking import ChunkingService


class TestChunkingService:
    """Test cases for ChunkingService."""
    
    def setup_method(self):
        """Set up test fixtures."""
        self.chunking_service = ChunkingService()
    
    def test_basic_chunking(self):
        """Test basic text chunking."""
        text = "This is a simple test text for chunking."
        chunks = self.chunking_service.chunk_text(
            text=text,
            chunk_size=20,
            chunk_overlap=5
        )
        
        assert len(chunks) > 1
        assert all("text" in chunk and "metadata" in chunk for chunk in chunks)
        assert all(chunk["metadata"]["chunk_index"] == i for i, chunk in enumerate(chunks))
    
    def test_text_shorter_than_chunk_size(self):
        """Test when text is shorter than chunk size."""
        text = "Short text."
        chunks = self.chunking_service.chunk_text(
            text=text,
            chunk_size=100,
            chunk_overlap=10
        )
        
        assert len(chunks) == 1
        assert chunks[0]["text"] == text
        assert chunks[0]["metadata"]["chunk_index"] == 0
        assert chunks[0]["metadata"]["chunk_size"] == len(text)
    
    def test_empty_text(self):
        """Test chunking empty text."""
        with pytest.raises(ValueError, match="text cannot be empty"):
            self.chunking_service.chunk_text(text="", chunk_size=512, chunk_overlap=64)
    
    def test_whitespace_only_text(self):
        """Test chunking whitespace-only text."""
        with pytest.raises(ValueError, match="text cannot be empty"):
            self.chunking_service.chunk_text(text="   ", chunk_size=512, chunk_overlap=64)
    
    def test_invalid_chunk_size(self):
        """Test invalid chunk size."""
        with pytest.raises(ValueError, match="chunk_size must be greater than 0"):
            self.chunking_service.chunk_text(text="test", chunk_size=0, chunk_overlap=0)
    
    def test_invalid_overlap(self):
        """Test invalid overlap."""
        with pytest.raises(ValueError, match="chunk_overlap must be non-negative"):
            self.chunking_service.chunk_text(text="test", chunk_size=512, chunk_overlap=-1)
    
    def test_overlap_greater_than_chunk_size(self):
        """Test when overlap >= chunk size."""
        with pytest.raises(ValueError, match="chunk_overlap must be less than chunk_size"):
            self.chunking_service.chunk_text(text="test", chunk_size=100, chunk_overlap=100)
    
    def test_zero_overlap(self):
        """Test chunking with zero overlap."""
        text = "This is a test text for zero overlap."
        chunks = self.chunking_service.chunk_text(
            text=text,
            chunk_size=20,
            chunk_overlap=0
        )
        
        assert len(chunks) >= 1
        for i, chunk in enumerate(chunks):
            assert chunk["metadata"]["chunk_index"] == i
            assert chunk["metadata"]["overlap"] == (0 if i == 0 else 0)
    
    def test_metadata_inheritance(self):
        """Test that metadata is properly inherited."""
        text = "Test text with metadata."
        metadata = {"source": "pdf", "author": "Test Author"}
        
        chunks = self.chunking_service.chunk_text(
            text=text,
            chunk_size=20,
            chunk_overlap=5,
            doc_id="doc_123",
            metadata=metadata
        )
        
        for chunk in chunks:
            assert chunk["metadata"]["source"] == "pdf"
            assert chunk["metadata"]["author"] == "Test Author"
            assert chunk["metadata"]["doc_id"] == "doc_123"
    
    def test_chunk_statistics(self):
        """Test chunking statistics."""
        text = "This is a test text for statistics calculation."
        stats = self.chunking_service.get_chunk_statistics(
            text=text,
            chunk_size=20,
            chunk_overlap=5
        )
        
        assert "total_chunks" in stats
        assert "total_characters" in stats
        assert "avg_chunk_size" in stats
        assert stats["total_characters"] == len(text)
    
    def test_chunk_statistics_empty_text(self):
        """Test statistics with empty text."""
        stats = self.chunking_service.get_chunk_statistics(
            text="",
            chunk_size=512,
            chunk_overlap=64
        )
        
        assert stats["total_chunks"] == 0
        assert stats["total_characters"] == 0
    
    def test_large_text_chunking(self):
        """Test chunking large text."""
        text = "word " * 200  # 1200 characters
        chunks = self.chunking_service.chunk_text(
            text=text,
            chunk_size=256,
            chunk_overlap=32
        )
        
        assert len(chunks) > 1
        # Verify overlap is working
        for i in range(1, len(chunks)):
            prev_chunk = chunks[i-1]["text"]
            curr_chunk = chunks[i]["text"]
            # Current chunk should start with some text from previous chunk
            overlap_size = chunks[i]["metadata"]["overlap"]
            assert curr_chunk.startswith(prev_chunk[-overlap_size:]) if overlap_size > 0 else True
    
    def test_exact_multiple_chunk_size(self):
        """Test when text length is exact multiple of step size."""
        step_size = 20  # chunk_size - overlap
        text = "a" * (step_size * 3)  # Exactly 3 chunks
        
        chunks = self.chunking_service.chunk_text(
            text=text,
            chunk_size=25,  # step_size = 20
            chunk_overlap=5
        )
        
        assert len(chunks) == 3
        assert sum(len(chunk["text"]) for chunk in chunks) >= len(text)