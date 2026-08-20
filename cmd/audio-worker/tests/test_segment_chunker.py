import pytest

from segment_chunker import SegmentChunker, _simple_chunk
from pkg.audio_client.models import AudioSegment, TranscriptionResult


class TestSegmentChunker:
    """Tests for SegmentChunker."""

    def test_basic_chunking_no_segments(self):
        """Test fallback to simple chunking when no segments."""
        result = TranscriptionResult(text="Short text")
        chunker = SegmentChunker(max_chars=50)
        chunks = chunker.chunk(result)

        assert len(chunks) == 1
        assert chunks[0]["text"] == "Short text"
        assert "chunk_id" in chunks[0]
        assert "start_offset" in chunks[0]
        assert "end_offset" in chunks[0]
        assert "token_count" in chunks[0]

    def test_diarized_segments_grouped_by_speaker(self):
        """Test chunking with diarized segments grouped by speaker."""
        segments = [
            AudioSegment(start=0.0, end=5.2, text="Hola, buenos días.", speaker="int1"),
            AudioSegment(start=5.3, end=10.1, text="Buenas, ¿en qué le puedo ayudar?", speaker="int2"),
            AudioSegment(start=10.2, end=15.0, text="Tengo un problema con mi factura.", speaker="int2"),
        ]
        result = TranscriptionResult(
            text="Full text",
            segments=segments,
        )
        chunker = SegmentChunker(max_chars=1500)
        chunks = chunker.chunk(result)

        assert len(chunks) == 2
        assert "[int1]: Hola, buenos días." in chunks[0]["text"]
        assert "[int2]: Buenas, ¿en qué le puedo ayudar? Tengo un problema con mi factura." in chunks[1]["text"]

    def test_max_char_limit_splits_chunk(self):
        """Test max char limit splits a single speaker turn into multiple chunks."""
        segments = [
            AudioSegment(start=0.0, end=10.0, text="A" * 2000, speaker="int1"),
        ]
        result = TranscriptionResult(text="Long text", segments=segments)
        chunker = SegmentChunker(max_chars=500)
        chunks = chunker.chunk(result)

        assert len(chunks) > 1

    def test_output_format_matches_chunk_text(self):
        """Test output format matches chunk_text() structure."""
        result = TranscriptionResult(text="Sample text for chunking")
        chunker = SegmentChunker()
        chunks = chunker.chunk(result)

        assert all(
            "chunk_id" in c and "text" in c and "start_offset" in c and "end_offset" in c and "token_count" in c
            for c in chunks
        )

    def test_simple_chunk_format(self):
        """Test _simple_chunk produces correct format."""
        chunks = _simple_chunk("Hello world test", 10)

        assert all(
            "chunk_id" in c and "text" in c and "start_offset" in c and "end_offset" in c and "token_count" in c
            for c in chunks
        )